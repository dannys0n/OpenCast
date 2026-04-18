#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CHEMISTRY_PATH="${1:-$ROOT_DIR/gsi/pipeline/chemistry_lines_v4.json}"

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "Usage: $(basename "$0") [path/to/chemistry_lines_v4.json]"
  exit 0
fi

python3 - "$ROOT_DIR" "$CHEMISTRY_PATH" <<'PY'
import json
import os
import random
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
        return "clone:scrawny_e0"
    if voice_name.startswith("clone:"):
        return voice_name
    return f"clone:{voice_name}"


def normalize_chemistry_sets(loaded):
    if not isinstance(loaded, list):
        return []

    normalized = []
    for item in loaded:
        if isinstance(item, list):
            normalized.append(item)
            continue
        if isinstance(item, dict) and isinstance(item.get("lines"), list):
            normalized.append(item["lines"])
    return normalized


def normalize_speaker(value):
    speaker = str(value or "").strip().lower()
    if speaker in {"caster0", "announcer", "play_by_play"}:
        return "announcer"
    if speaker in {"caster1", "turret", "color"}:
        return "turret"
    return "announcer"


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


def main():
    if shutil.which("play") is None:
        raise RuntimeError("SoX 'play' command not found in PATH")

    root_dir = Path(sys.argv[1]).resolve()
    chemistry_path = Path(sys.argv[2]).resolve()
    if not chemistry_path.exists():
        raise RuntimeError(f"chemistry file not found: {chemistry_path}")

    text_llm_env = load_env_file(root_dir / "gsi" / "pipeline" / ".env")
    tts_env = load_env_file(root_dir / ".env")

    api_base = first_value("TTS_API_BASE", "http://127.0.0.1:8880", tts_env, text_llm_env).rstrip("/")
    model = first_value("TTS_MODEL", "tts-1", tts_env, text_llm_env)
    timeout_seconds = float(first_value("TTS_TIMEOUT", "120", tts_env, text_llm_env))
    sample_rate = int(first_value("TTS_SAMPLE_RATE", "24000", tts_env, text_llm_env))
    default_voice = normalize_voice_name(
        first_value(
            "TTS_DEFAULT_VOICE_NAME",
            first_value("VOICE_NAME", "clone:announcer_e0", tts_env, text_llm_env),
            tts_env,
            text_llm_env,
        )
    )
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
    if not chemistry_sets:
        raise RuntimeError(f"no chemistry bundles found in {chemistry_path}")

    bundle = random.choice(chemistry_sets)
    print(f"[chemistry] picked random bundle from {chemistry_path}")
    for line in bundle:
        speaker = normalize_speaker(line.get("caster") or line.get("speaker") or "announcer")
        text = " ".join(str(line.get("text") or "").split()).strip()
        if not text:
            continue

        print(f"[{speaker}] {text}", flush=True)
        payload = {
            "model": model,
            "voice": announcer_voice if speaker == "announcer" else turret_voice,
            "input": text,
            "instruct": chemistry_instruct(speaker),
            "speed": announcer_speed if speaker == "announcer" else turret_speed,
            "stream": True,
            "response_format": "pcm",
        }
        stream_tts(
            api_base=api_base,
            timeout_seconds=timeout_seconds,
            sample_rate=sample_rate,
            payload=payload,
            speed=payload["speed"],
        )


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise SystemExit(f"TTS HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise SystemExit(f"TTS request failed: {error}") from error
    except Exception as error:
        raise SystemExit(str(error)) from error
PY
