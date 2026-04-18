#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEXT_LLM_ENV_FILE="$ROOT_DIR/../text-llm/.env"
CHEMISTRY_PATH="${1:-$ROOT_DIR/gsi/pipeline/chemistry_lines_v4.json}"

if [[ -f "$TEXT_LLM_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$TEXT_LLM_ENV_FILE"
  set +a
fi

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: $(basename "$0") [path/to/chemistry_lines_v4.json]"
  exit 0
fi

python3 - "$ROOT_DIR" "$CHEMISTRY_PATH" <<'PY'
import json
import os
import random
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


def load_env_file(path):
    values = {}
    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value
    return values


def first_value(name, default, *sources):
    if name in os.environ:
        return os.environ[name]
    for source in sources:
        if name in source:
            return source[name]
    return default


def normalize_voice_name(raw_voice_name):
    voice_name = str(raw_voice_name or "").strip()
    if not voice_name:
        return "clone:announcer_e0"
    if voice_name.startswith("clone:"):
        return voice_name
    return f"clone:{voice_name}"


def normalize_speaker(value):
    speaker = str(value or "").strip().lower()
    if speaker in {"caster0", "announcer", "play_by_play"}:
        return "announcer"
    if speaker in {"caster1", "turret", "color"}:
        return "turret"
    return "announcer"


def normalize_chemistry_sets(loaded):
    if not isinstance(loaded, list):
        return []

    normalized = []
    for item in loaded:
        if isinstance(item, list):
            lines = []
            for raw_line in item:
                if not isinstance(raw_line, dict):
                    continue
                speaker = normalize_speaker(raw_line.get("speaker") or raw_line.get("caster"))
                text = " ".join(str(raw_line.get("text") or "").split()).strip()
                if text:
                    lines.append({"speaker": speaker, "text": text})
            if lines:
                normalized.append(lines)
            continue

        if isinstance(item, dict) and isinstance(item.get("lines"), list):
            lines = []
            for raw_line in item["lines"]:
                if not isinstance(raw_line, dict):
                    continue
                speaker = normalize_speaker(raw_line.get("speaker") or raw_line.get("caster"))
                text = " ".join(str(raw_line.get("text") or "").split()).strip()
                if text:
                    lines.append({"speaker": speaker, "text": text})
            if lines:
                normalized.append(lines)
    return normalized


def extract_message_content(response_json):
    choices = response_json.get("choices") or []
    if not choices:
        raise RuntimeError("text model returned no choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("text model returned empty content")
    content = content.strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
    return content


def parse_generated_bundle(raw_text):
    text = str(raw_text or "").strip()
    parsed = None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            parsed = json.loads(text[start : end + 1])

    if not isinstance(parsed, list):
        raise RuntimeError("text model did not return a JSON array")

    lines = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        speaker = normalize_speaker(item.get("speaker") or item.get("caster"))
        text = " ".join(str(item.get("text") or "").split()).strip()
        if text:
            lines.append({"speaker": speaker, "text": text})

    if not lines:
        raise RuntimeError("text model returned no usable chemistry lines")
    return lines


def chemistry_instruct(speaker):
    if speaker == "announcer":
        return "Deliver it as quick, sharp, slightly smug banter."
    return "Deliver it as dry, clinical, slightly disdainful banter."


def open_player(sample_rate, speed):
    command = [
        "play",
        "-q",
        "-t",
        "raw",
        "-b",
        "16",
        "-e",
        "signed-integer",
        "-c",
        "1",
        "-r",
        str(sample_rate),
        "-",
    ]
    if abs(float(speed) - 1.0) > 0.01:
        command.extend(["tempo", f"{float(speed):.3f}"])
    return subprocess.Popen(command, stdin=subprocess.PIPE)


def stream_tts(api_base, timeout_seconds, sample_rate, payload, speed):
    request = urllib.request.Request(
        f"{api_base}/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    player = None
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            player = open_player(sample_rate, speed)
            if player.stdin is None:
                raise RuntimeError("failed to open stdin for SoX play")

            while True:
                chunk = response.read(16384)
                if not chunk:
                    break
                player.stdin.write(chunk)
                player.stdin.flush()

        player.stdin.close()
        player.stdin = None
        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")
    finally:
        if player is not None:
            if player.stdin is not None:
                try:
                    player.stdin.close()
                except Exception:
                    pass
            if player.poll() is None:
                player.kill()
                player.wait()


def request_new_bundle(model_api_base, model_name, temperature, max_tokens, timeout_seconds, few_shots):
    system_prompt = (
        "You write short in-character exchanges between two Portal-2-inspired voices named announcer and turret. "
        "The tone is dry, playful, slightly menacing, and absurdly polite. "
        "Do not mention Counter-Strike, rounds, maps, sites, utility, score, players, combat state, or live match context. "
        "Return JSON only. "
        "Return exactly one top-level array. "
        'Each item must be an object with keys "speaker" and "text". '
        'speaker must be "announcer" or "turret". '
        "text must be one short spoken line. "
        "Generate exactly 3 lines total."
    )

    user_prompt = (
        "Using the few-shot examples below, generate one new chemistry bundle.\n"
        "Keep it self-contained banter only.\n"
        "You may echo the personality and style of the examples, but do not copy them.\n"
        "At least one line should feel like a subtle nod to Portal 2 voice writing.\n\n"
        "Few-shot examples JSON:\n"
        f"{json.dumps(few_shots, indent=2)}\n\n"
        "Return JSON like:\n"
        '[{"speaker":"announcer","text":"..."},{"speaker":"turret","text":"..."},{"speaker":"announcer","text":"..."}]'
    ).rstrip() + "\n/no_think"

    request_body = {
        "model": model_name,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }

    request = urllib.request.Request(
        f"{model_api_base.rstrip('/')}/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"text model HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"text model request failed: {error}") from error

    raw_text = extract_message_content(response_json)
    return {
        "raw_text": raw_text,
        "request": request_body,
        "lines": parse_generated_bundle(raw_text),
    }


def main():
    if shutil.which("play") is None:
        raise RuntimeError("SoX 'play' command not found in PATH")

    root_dir = Path(sys.argv[1]).resolve()
    chemistry_path = Path(sys.argv[2]).resolve()
    if not chemistry_path.exists():
        raise RuntimeError(f"chemistry file not found: {chemistry_path}")

    text_llm_env = load_env_file(root_dir / "gsi" / "pipeline" / ".env")
    tts_env = load_env_file(root_dir / ".env")

    model_api_base = first_value("MODEL_API_BASE", "http://127.0.0.1:12434", text_llm_env)
    model_name = first_value("MODEL_NAME", "hf.co/unsloth/Qwen3-1.7B-GGUF:Q4_K_M", text_llm_env)
    if model_name.endswith("/no_think"):
        model_name = model_name[:-9]
    model_temperature = float(first_value("TEMPERATURE", "0.8", text_llm_env))
    model_max_tokens = int(first_value("MAX_TOKENS", "240", text_llm_env))
    model_timeout = float(first_value("MODEL_TIMEOUT", "45", text_llm_env))

    tts_api_base = first_value("TTS_API_BASE", "http://127.0.0.1:8880", tts_env, text_llm_env).rstrip("/")
    tts_model = first_value("TTS_MODEL", "tts-1", tts_env, text_llm_env)
    tts_timeout = float(first_value("TTS_TIMEOUT", "120", tts_env, text_llm_env))
    sample_rate = int(first_value("TTS_SAMPLE_RATE", "24000", tts_env, text_llm_env))
    announcer_voice = normalize_voice_name(
        first_value(
            "ANNOUNCER_VOICE_NAME",
            first_value(
                "V4_PLAY_BY_PLAY_VOICE_NAME",
                first_value("PLAY_BY_PLAY_VOICE_NAME", "clone:announcer_e0", tts_env, text_llm_env),
                tts_env,
                text_llm_env,
            ),
            tts_env,
            text_llm_env,
        )
    )
    turret_voice = normalize_voice_name(
        first_value(
            "TURRET_VOICE_NAME",
            first_value(
                "V4_COLOR_VOICE_NAME",
                first_value("COLOR_VOICE_NAME", "clone:turret_e0", tts_env, text_llm_env),
                tts_env,
                text_llm_env,
            ),
            tts_env,
            text_llm_env,
        )
    )
    announcer_speed = float(first_value("V4_PLAY_BY_PLAY_SPEED", "1.08", tts_env, text_llm_env))
    turret_speed = float(first_value("V4_COLOR_SPEED", "1.0", tts_env, text_llm_env))

    loaded = json.loads(chemistry_path.read_text(encoding="utf-8"))
    chemistry_sets = normalize_chemistry_sets(loaded)
    if len(chemistry_sets) < 2:
        raise RuntimeError("need at least 2 chemistry bundles for few-shot generation")

    few_shot_count = min(4, len(chemistry_sets))
    few_shots = random.sample(chemistry_sets, few_shot_count)
    generation = request_new_bundle(
        model_api_base=model_api_base,
        model_name=model_name,
        temperature=model_temperature,
        max_tokens=model_max_tokens,
        timeout_seconds=model_timeout,
        few_shots=few_shots,
    )

    print(f"[chemistry] generated new bundle via {model_api_base}")
    print("[chemistry] few-shot examples used:")
    for index, shot in enumerate(few_shots, start=1):
        print(f"  example {index}:")
        for line in shot:
            print(f"    [{line['speaker']}] {line['text']}")

    print("[chemistry] generated lines:")
    for line in generation["lines"]:
        print(f"  [{line['speaker']}] {line['text']}")

    for line in generation["lines"]:
        speaker = line["speaker"]
        text = line["text"]
        payload = {
            "model": tts_model,
            "voice": announcer_voice if speaker == "announcer" else turret_voice,
            "input": text,
            "instruct": chemistry_instruct(speaker),
            "speed": announcer_speed if speaker == "announcer" else turret_speed,
            "stream": True,
            "response_format": "pcm",
        }
        stream_tts(
            api_base=tts_api_base,
            timeout_seconds=tts_timeout,
            sample_rate=sample_rate,
            payload=payload,
            speed=payload["speed"],
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        raise SystemExit(str(error)) from error
PY
