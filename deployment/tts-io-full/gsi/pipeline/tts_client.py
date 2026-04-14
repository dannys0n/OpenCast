import json
import os
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
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


@dataclass
class TTSConfig:
    api_base: str
    model: str
    voice_name: str
    timeout_seconds: float
    sample_rate: int


def normalize_voice_name(raw_voice_name):
    voice_name = (raw_voice_name or "").strip()
    if not voice_name:
        return "clone:scrawny_e0"
    if voice_name.startswith("clone:"):
        return voice_name
    return f"clone:{voice_name}"


def build_config(repo_root):
    repo_root = Path(repo_root).resolve()
    text_llm_env = load_env_file(repo_root / "deployment" / "text-llm" / ".env")
    tts_env = load_env_file(repo_root / "deployment" / "tts-io-full" / ".env")

    api_base = first_value("TTS_API_BASE", "http://127.0.0.1:8880", tts_env, text_llm_env)
    model = first_value("TTS_MODEL", "tts-1", tts_env, text_llm_env)
    voice_name = normalize_voice_name(
        first_value("TTS_DEFAULT_VOICE_NAME", first_value("VOICE_NAME", "clone:scrawny_e0", tts_env, text_llm_env), tts_env, text_llm_env)
    )
    timeout_seconds = float(first_value("TTS_TIMEOUT", "120", tts_env, text_llm_env))
    sample_rate = int(first_value("TTS_SAMPLE_RATE", "24000", tts_env, text_llm_env))

    return TTSConfig(
        api_base=api_base.rstrip("/"),
        model=model,
        voice_name=voice_name,
        timeout_seconds=timeout_seconds,
        sample_rate=sample_rate,
    )


def build_tts_instruct(tts_prompt):
    emotion = (tts_prompt.get("emotion") or "").strip().lower()
    caster = (tts_prompt.get("caster") or "").strip().lower()

    if emotion == "screaming":
        emotion_text = "Speak with intense hype and loud urgent energy."
    elif emotion == "excited":
        emotion_text = "Speak with energetic excitement and forward momentum."
    else:
        emotion_text = "Speak with a calm measured tone."

    if caster == "play_by_play":
        caster_text = "Deliver it as rapid play-by-play commentary."
    else:
        caster_text = "Deliver it as reflective color commentary."

    return f"{caster_text} {emotion_text}"


def build_tts_payload(config, tts_prompt):
    speed = float(tts_prompt.get("speed") or 1.0)
    return {
        "model": config.model,
        "voice": normalize_voice_name(tts_prompt.get("voice_name") or config.voice_name),
        "input": tts_prompt["commentary"],
        "instruct": build_tts_instruct(tts_prompt),
        "speed": speed,
        "stream": True,
        "response_format": "pcm",
    }


def play_pcm_stream(response, sample_rate, speed=1.0):
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

    player = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
    )

    try:
        if player.stdin is None:
            raise RuntimeError("failed to open stdin for SoX play")

        while True:
            chunk = response.read(16384)
            if not chunk:
                break
            player.stdin.write(chunk)
            player.stdin.flush()
    finally:
        if player.stdin is not None:
            player.stdin.close()
        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")


def open_play_process(sample_rate, speed=1.0):
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

    return subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
    )


def fetch_tts_audio_to_file(config, tts_prompt, buffer_path, result, cancel_event=None):
    payload = build_tts_payload(config, tts_prompt)
    request = urllib.request.Request(
        f"{config.api_base}/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            with buffer_path.open("wb") as handle:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        result["ok"] = False
                        result["cancelled"] = True
                        break
                    chunk = response.read(16384)
                    if not chunk:
                        break
                    handle.write(chunk)
                    handle.flush()
        if result.get("cancelled"):
            result["ok"] = False
        else:
            result["ok"] = True
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        result["ok"] = False
        result["error"] = f"TTS HTTP {error.code}: {body}"
    except urllib.error.URLError as error:
        result["ok"] = False
        result["error"] = f"TTS request failed: {error}"
    except Exception as error:
        result["ok"] = False
        result["error"] = str(error)
    finally:
        result["done"] = True


def stream_buffer_file_to_stdin(player_stdin, buffer_path, result):
    offset = 0

    while True:
        size = buffer_path.stat().st_size if buffer_path.exists() else 0
        if size > offset:
            with buffer_path.open("rb") as handle:
                handle.seek(offset)
                while True:
                    chunk = handle.read(min(16384, size - offset))
                    if not chunk:
                        break
                    player_stdin.write(chunk)
                    player_stdin.flush()
                    offset += len(chunk)
            continue

        if result.get("done"):
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or f"TTS request failed for {buffer_path.name}")
            break

        time.sleep(0.01)


def stream_tts_sequence_playback(config, tts_prompts):
    if not tts_prompts:
        return {"line_count": 0}

    speeds = {float(prompt.get("speed") or 1.0) for prompt in tts_prompts}
    if len(speeds) != 1:
        raise RuntimeError("all TTS prompts in a sequence must use the same speed")
    speed = next(iter(speeds))

    workers = []
    player = None
    temp_dir_obj = tempfile.TemporaryDirectory(prefix="gsi_tts_sequence_")

    try:
        temp_dir = Path(temp_dir_obj.name)
        for index, tts_prompt in enumerate(tts_prompts):
            buffer_path = temp_dir / f"request_{index}.pcm"
            buffer_path.touch()
            result = {
                "done": False,
                "ok": False,
                "line_index": index,
                "commentary": tts_prompt.get("commentary"),
            }
            thread = threading.Thread(
                target=fetch_tts_audio_to_file,
                args=(config, tts_prompt, buffer_path, result),
                daemon=True,
            )
            thread.start()
            workers.append(
                {
                    "thread": thread,
                    "buffer_path": buffer_path,
                    "result": result,
                }
            )

        player = open_play_process(config.sample_rate, speed=speed)
        if player.stdin is None:
            raise RuntimeError("failed to open stdin for SoX play")

        for worker in workers:
            stream_buffer_file_to_stdin(player.stdin, worker["buffer_path"], worker["result"])

        player.stdin.close()
        player.stdin = None

        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")

        for worker in workers:
            worker["thread"].join()
            if not worker["result"].get("ok"):
                raise RuntimeError(worker["result"].get("error") or "TTS sequence request failed")

        return {
            "line_count": len(tts_prompts),
            "speed": speed,
        }
    finally:
        for worker in workers:
            worker["thread"].join()

        if player is not None:
            if player.stdin is not None:
                try:
                    player.stdin.close()
                except Exception:
                    pass
            if player.poll() is None:
                player.kill()
                player.wait()

        temp_dir_obj.cleanup()


def stream_tts_playback(config, tts_prompt):
    speed = float(tts_prompt.get("speed") or 1.0)
    payload = build_tts_payload(config, tts_prompt)

    request = urllib.request.Request(
        f"{config.api_base}/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            play_pcm_stream(response, config.sample_rate, speed=speed)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"TTS HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"TTS request failed: {error}") from error
