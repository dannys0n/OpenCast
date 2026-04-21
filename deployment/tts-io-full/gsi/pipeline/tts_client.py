import json
import os
import queue
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
    omnivoice_num_step: int | None = None
    omnivoice_guidance_scale: float | None = None
    omnivoice_denoise: bool | None = None
    omnivoice_t_shift: float | None = None
    omnivoice_position_temperature: float | None = None
    omnivoice_class_temperature: float | None = None
    omnivoice_duration: float | None = None
    omnivoice_language: str | None = None
    omnivoice_layer_penalty_factor: float | None = None
    omnivoice_preprocess_prompt: bool | None = None
    omnivoice_postprocess_output: bool | None = None
    omnivoice_audio_chunk_duration: float | None = None
    omnivoice_audio_chunk_threshold: float | None = None


def normalize_voice_name(raw_voice_name):
    voice_name = (raw_voice_name or "").strip()
    if not voice_name:
        return "clone:scrawny_e0"
    if voice_name.startswith("clone:"):
        return voice_name
    return f"clone:{voice_name}"


def env_optional_float(name, *sources):
    value = first_value(name, "", *sources)
    if value in ("", None):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def env_optional_int(name, *sources):
    value = first_value(name, "", *sources)
    if value in ("", None):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def env_optional_bool(name, *sources):
    value = first_value(name, "", *sources)
    if value in ("", None):
        return None
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


def env_optional_text(name, *sources):
    value = first_value(name, "", *sources)
    if value in ("", None):
        return None
    text = str(value).strip()
    return text or None


def build_config(repo_root):
    repo_root = Path(repo_root).resolve()
    text_llm_env = load_env_file(repo_root / "deployment" / "text-llm" / ".env")
    tts_env = load_env_file(repo_root / "deployment" / "tts-io-full" / ".env")
    omnivoice_env = load_env_file(repo_root / "deployment" / "tts-io-full" / "omnivoice-server" / ".env")
    omnivoice_opencast_env = load_env_file(repo_root / "deployment" / "tts-io-full" / "omnivoice-server" / ".opencast.env")

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
        omnivoice_num_step=env_optional_int("OMNIVOICE_TTS_NUM_STEP", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_guidance_scale=env_optional_float("OMNIVOICE_TTS_GUIDANCE_SCALE", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_denoise=env_optional_bool("OMNIVOICE_TTS_DENOISE", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_t_shift=env_optional_float("OMNIVOICE_TTS_T_SHIFT", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_position_temperature=env_optional_float("OMNIVOICE_TTS_POSITION_TEMPERATURE", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_class_temperature=env_optional_float("OMNIVOICE_TTS_CLASS_TEMPERATURE", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_duration=env_optional_float("OMNIVOICE_TTS_DURATION", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_language=env_optional_text("OMNIVOICE_TTS_LANGUAGE", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_layer_penalty_factor=env_optional_float("OMNIVOICE_TTS_LAYER_PENALTY_FACTOR", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_preprocess_prompt=env_optional_bool("OMNIVOICE_TTS_PREPROCESS_PROMPT", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_postprocess_output=env_optional_bool("OMNIVOICE_TTS_POSTPROCESS_OUTPUT", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_audio_chunk_duration=env_optional_float("OMNIVOICE_TTS_AUDIO_CHUNK_DURATION", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
        omnivoice_audio_chunk_threshold=env_optional_float("OMNIVOICE_TTS_AUDIO_CHUNK_THRESHOLD", omnivoice_opencast_env, omnivoice_env, tts_env, text_llm_env),
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

    if caster in {"play_by_play", "caster0"}:
        caster_text = "Deliver it as rapid play-by-play commentary."
    else:
        caster_text = "Deliver it as reflective color commentary."

    return f"{caster_text} {emotion_text}"


def build_tts_payload(config, tts_prompt):
    speed = float(tts_prompt.get("speed") or 1.0)
    payload = {
        "model": config.model,
        "voice": normalize_voice_name(tts_prompt.get("voice_name") or config.voice_name),
        "input": tts_prompt["commentary"],
        "instruct": build_tts_instruct(tts_prompt),
        "instructions": build_tts_instruct(tts_prompt),
        "speed": speed,
        "stream": True,
        "response_format": "pcm",
    }
    optional_fields = {
        "num_step": config.omnivoice_num_step,
        "guidance_scale": config.omnivoice_guidance_scale,
        "denoise": config.omnivoice_denoise,
        "t_shift": config.omnivoice_t_shift,
        "position_temperature": config.omnivoice_position_temperature,
        "class_temperature": config.omnivoice_class_temperature,
        "duration": config.omnivoice_duration,
        "language": config.omnivoice_language,
        "layer_penalty_factor": config.omnivoice_layer_penalty_factor,
        "preprocess_prompt": config.omnivoice_preprocess_prompt,
        "postprocess_output": config.omnivoice_postprocess_output,
        "audio_chunk_duration": config.omnivoice_audio_chunk_duration,
        "audio_chunk_threshold": config.omnivoice_audio_chunk_threshold,
    }
    for key, value in optional_fields.items():
        if value is not None:
            payload[key] = value
    return payload


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
    request_started_at = time.monotonic()
    result["request_started_at_monotonic"] = request_started_at
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
                    if "first_pcm_latency_seconds" not in result:
                        result["first_pcm_latency_seconds"] = time.monotonic() - request_started_at
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


def fetch_tts_audio_to_queue(config, tts_prompt, chunk_queue, result, cancel_event=None, bytes_per_frame=2):
    payload = build_tts_payload(config, tts_prompt)
    request_started_at = time.monotonic()
    result["request_started_at_monotonic"] = request_started_at
    request = urllib.request.Request(
        f"{config.api_base}/v1/audio/speech",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    carry = b""
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    result["ok"] = False
                    result["cancelled"] = True
                    break

                chunk = response.read(16384)
                if not chunk:
                    break
                if "first_pcm_latency_seconds" not in result:
                    result["first_pcm_latency_seconds"] = time.monotonic() - request_started_at

                data = carry + chunk
                remainder = len(data) % bytes_per_frame
                if remainder:
                    carry = data[-remainder:]
                    data = data[:-remainder]
                else:
                    carry = b""

                if not data:
                    continue

                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        result["ok"] = False
                        result["cancelled"] = True
                        break
                    try:
                        chunk_queue.put(data, timeout=0.1)
                        result["bytes_written"] = int(result.get("bytes_written") or 0) + len(data)
                        break
                    except queue.Full:
                        continue

                if result.get("cancelled"):
                    break

        if carry:
            result["truncated_bytes"] = len(carry)

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
        try:
            chunk_queue.put_nowait(None)
        except queue.Full:
            pass


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


def stream_prefetched_tts_playback_interruptibly(
    config,
    tts_prompt,
    buffer_path,
    fetch_result,
    interrupt_event,
    *,
    startup_timeout_seconds=8.0,
    stall_timeout_seconds=5.0,
    max_playback_seconds=None,
):
    bytes_per_frame = 2
    player = None
    interrupted = False
    offset = 0
    started_at = time.monotonic()
    last_progress_at = started_at
    playback_seconds = 0.0

    if max_playback_seconds is None:
        commentary = str(tts_prompt.get("commentary") or "")
        max_playback_seconds = max(8.0, min(30.0, 4.0 + (len(commentary) * 0.09)))

    def close_player_immediately():
        nonlocal player
        if player is None:
            return
        if player.stdin is not None:
            try:
                player.stdin.close()
            except Exception:
                pass
            player.stdin = None
        if player.poll() is None:
            try:
                player.kill()
            except Exception:
                pass
            try:
                player.wait(timeout=0.5)
            except Exception:
                pass

    try:
        player = open_play_process(config.sample_rate, speed=float(tts_prompt.get("speed") or 1.0))
        if player.stdin is None:
            raise RuntimeError("failed to open stdin for SoX play")

        while True:
            if interrupt_event.is_set():
                interrupted = True
                return {"interrupted": True, "fetch_result": dict(result)}

            size = buffer_path.stat().st_size if buffer_path.exists() else 0
            available = size - offset
            playable = available - (available % bytes_per_frame)

            if playable > 0:
                with buffer_path.open("rb") as handle:
                    handle.seek(offset)
                    remaining = playable
                    while remaining > 0:
                        if interrupt_event.is_set():
                            interrupted = True
                            return {"interrupted": True}
                        chunk = handle.read(min(16384, remaining))
                        if not chunk:
                            break
                        if len(chunk) % bytes_per_frame != 0:
                            interrupt_event.set()
                            raise RuntimeError("prefetched TTS buffer returned misaligned PCM data")
                        player.stdin.write(chunk)
                        player.stdin.flush()
                        offset += len(chunk)
                        remaining -= len(chunk)
                        last_progress_at = time.monotonic()
                        playback_seconds += len(chunk) / (config.sample_rate * bytes_per_frame)
                        if playback_seconds > max_playback_seconds:
                            interrupt_event.set()
                            raise RuntimeError(
                                f"TTS playback exceeded safety limit of {max_playback_seconds:.1f}s"
                            )
                continue

            if fetch_result.get("done"):
                if not fetch_result.get("ok"):
                    if fetch_result.get("cancelled"):
                        interrupted = True
                        return {"interrupted": True}
                    raise RuntimeError(fetch_result.get("error") or "TTS request failed")
                break

            now = time.monotonic()
            if playback_seconds == 0.0 and now - started_at > startup_timeout_seconds:
                interrupt_event.set()
                raise RuntimeError("prefetched TTS stream timed out before any audio arrived")
            if playback_seconds > 0.0 and now - last_progress_at > stall_timeout_seconds:
                interrupt_event.set()
                raise RuntimeError("prefetched TTS stream stalled during playback")

            time.sleep(0.01)

        player.stdin.close()
        player.stdin = None
        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")
        return {"interrupted": False}
    finally:
        close_player_immediately()
        if interrupt_event.is_set():
            interrupted = True


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


def stream_tts_playback_interruptibly(
    config,
    tts_prompt,
    interrupt_event,
    *,
    startup_timeout_seconds=8.0,
    stall_timeout_seconds=5.0,
    max_playback_seconds=None,
):
    bytes_per_frame = 2
    chunk_queue = queue.Queue(maxsize=32)
    result = {"done": False, "ok": False}
    player = None
    thread = None
    interrupted = False
    started_at = time.monotonic()
    last_progress_at = started_at
    playback_seconds = 0.0

    if max_playback_seconds is None:
        commentary = str(tts_prompt.get("commentary") or "")
        max_playback_seconds = max(8.0, min(30.0, 4.0 + (len(commentary) * 0.09)))

    def close_player_immediately():
        nonlocal player
        if player is None:
            return
        if player.stdin is not None:
            try:
                player.stdin.close()
            except Exception:
                pass
            player.stdin = None
        if player.poll() is None:
            try:
                player.kill()
            except Exception:
                pass
            try:
                player.wait(timeout=0.5)
            except Exception:
                pass

    try:
        thread = threading.Thread(
            target=fetch_tts_audio_to_queue,
            args=(config, tts_prompt, chunk_queue, result, interrupt_event, bytes_per_frame),
            daemon=True,
        )
        thread.start()

        player = open_play_process(config.sample_rate, speed=float(tts_prompt.get("speed") or 1.0))
        if player.stdin is None:
            raise RuntimeError("failed to open stdin for SoX play")

        while True:
            if interrupt_event.is_set():
                interrupted = True
                return {"interrupted": True}

            try:
                chunk = chunk_queue.get(timeout=0.05)
            except queue.Empty:
                now = time.monotonic()
                if playback_seconds == 0.0 and now - started_at > startup_timeout_seconds:
                    interrupt_event.set()
                    raise RuntimeError("TTS stream timed out before any audio arrived")
                if playback_seconds > 0.0 and now - last_progress_at > stall_timeout_seconds:
                    interrupt_event.set()
                    raise RuntimeError("TTS stream stalled during playback")
                continue

            if chunk is None:
                if result.get("done"):
                    if not result.get("ok"):
                        if result.get("cancelled"):
                            interrupted = True
                            return {"interrupted": True, "fetch_result": dict(result)}
                        raise RuntimeError(result.get("error") or "TTS request failed")
                    break
                continue

            if len(chunk) % bytes_per_frame != 0:
                interrupt_event.set()
                raise RuntimeError("TTS stream returned misaligned PCM data")

            player.stdin.write(chunk)
            player.stdin.flush()
            last_progress_at = time.monotonic()
            playback_seconds += len(chunk) / (config.sample_rate * bytes_per_frame)

            if playback_seconds > max_playback_seconds:
                interrupt_event.set()
                raise RuntimeError(f"TTS playback exceeded safety limit of {max_playback_seconds:.1f}s")

        player.stdin.close()
        player.stdin = None
        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")
        return {"interrupted": False, "fetch_result": dict(result)}
    finally:
        close_player_immediately()
        if interrupt_event.is_set():
            interrupted = True
        if interrupted:
            cleanup_thread = threading.Thread(
                target=thread.join,
                daemon=True,
                name="gsi-v3-tts-cleanup",
            ) if thread is not None else None
            if cleanup_thread is not None:
                cleanup_thread.start()
        elif thread is not None:
            thread.join()
