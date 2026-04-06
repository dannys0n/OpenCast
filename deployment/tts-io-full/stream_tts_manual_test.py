#!/usr/bin/env python3
"""Direct streaming TTS entrypoint backed by the local Qwen3-TTS-streaming fork."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
VENV_PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

if VENV_PYTHON.exists() and Path(sys.executable).resolve() != VENV_PYTHON.resolve():
    os.execv(str(VENV_PYTHON), [str(VENV_PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]])

import numpy as np
import soundfile as sf
import torch

DEFAULT_STREAMING_REPO = SCRIPT_DIR / "Qwen3-TTS-streaming"
DEFAULT_MODEL_PATH = Path(
    "/home/danny/Desktop/vLLM-Omni/.hf-cache/hub/models--Qwen--Qwen3-TTS-12Hz-0.6B-Base/"
    "snapshots/5d83992436eae1d760afd27aff78a71d676296fc"
)
DEFAULT_VOICES_DIR = SCRIPT_DIR / "voices"


def load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_dotenv(SCRIPT_DIR / ".env")

STREAMING_REPO = Path(os.environ.get("TTS_QWEN_STREAMING_REPO_PATH", str(DEFAULT_STREAMING_REPO))).expanduser().resolve()
if str(STREAMING_REPO) not in sys.path:
    sys.path.insert(0, str(STREAMING_REPO))

from qwen_tts import Qwen3TTSModel


def normalize_voice_name(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return value.strip("_")


def discover_voices(voices_dir: Path) -> dict[str, dict[str, Path | str | None]]:
    result: dict[str, dict[str, Path | str | None]] = {}
    if not voices_dir.is_dir():
        return result

    for audio_path in sorted(voices_dir.iterdir()):
        if not audio_path.is_file():
            continue
        if audio_path.suffix.lower() not in {".wav", ".mp3", ".flac", ".m4a", ".ogg"}:
            continue
        voice_name = normalize_voice_name(audio_path.stem)
        transcript_path = audio_path.with_suffix(".txt")
        transcript_text = transcript_path.read_text(encoding="utf-8").strip() if transcript_path.is_file() else None
        result[voice_name] = {
            "audio_path": audio_path,
            "transcript_path": transcript_path if transcript_path.is_file() else None,
            "transcript_text": transcript_text,
        }
    return result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Direct sample+transcript streaming TTS with Qwen3-TTS-streaming.")
    parser.add_argument("text", nargs="*", help="Text to speak. If omitted, text is read from stdin.")
    parser.add_argument("--voice-name", default=None, help="Voice name derived from voices/*.wav filenames.")
    parser.add_argument("--list-voices", action="store_true", help="Print discovered local voices and exit.")
    parser.add_argument("--voices-dir", default=str(DEFAULT_VOICES_DIR), help=f"Directory containing voice wav/txt pairs (default: {DEFAULT_VOICES_DIR})")
    parser.add_argument("--model-path", default=None, help="Local model snapshot path or Hugging Face model id.")
    parser.add_argument("--language", default=os.environ.get("TTS_LANGUAGE", "English"))
    parser.add_argument("--output", default=None, help="Optional WAV output path.")
    parser.add_argument("--x-vector-only", action="store_true", help="Use speaker embedding only instead of sample+transcript ICL mode.")
    parser.add_argument("--emit-every-frames", type=int, default=int(os.environ.get("TTS_EMIT_EVERY_FRAMES", "8")))
    parser.add_argument("--decode-window-frames", type=int, default=int(os.environ.get("TTS_DECODE_WINDOW_FRAMES", "80")))
    parser.add_argument("--first-chunk-emit-every", type=int, default=int(os.environ.get("TTS_FIRST_CHUNK_EMIT_EVERY", "0")))
    parser.add_argument("--first-chunk-decode-window", type=int, default=int(os.environ.get("TTS_FIRST_CHUNK_DECODE_WINDOW", "48")))
    parser.add_argument("--first-chunk-frames", type=int, default=int(os.environ.get("TTS_FIRST_CHUNK_FRAMES", "48")))
    parser.add_argument("--repetition-penalty", type=float, default=float(os.environ.get("TTS_REPETITION_PENALTY", "1.0")))
    parser.add_argument("--enable-optimizations", action="store_true", default=os.environ.get("TTS_ENABLE_STREAMING_OPTIMIZATIONS", "0") == "1")
    parser.add_argument("--attn-implementation", default=os.environ.get("TTS_ATTN_IMPLEMENTATION", "eager"))
    return parser.parse_args()


def get_text(args: argparse.Namespace) -> str:
    if args.text:
        return " ".join(args.text).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide text as an argument or pipe it into stdin.")


def resolve_model_path(arg_value: str | None) -> str:
    if arg_value:
        return arg_value
    env_path = os.environ.get("TTS_LOCAL_MODEL_PATH")
    if env_path:
        return env_path
    env_name = os.environ.get("TTS_MODEL_NAME")
    if env_name:
        return env_name
    return str(DEFAULT_MODEL_PATH)


def resolve_voice_name(args: argparse.Namespace, voices: dict[str, dict[str, Path | str | None]]) -> str:
    configured = args.voice_name or os.environ.get("TTS_DEFAULT_VOICE_NAME")
    if configured:
        normalized = normalize_voice_name(configured)
        if normalized in voices:
            return normalized
        available = ", ".join(sorted(voices)) if voices else "(none)"
        raise SystemExit(f"Unknown voice {configured!r}. Available voices: {available}")
    if len(voices) == 1:
        return next(iter(voices))
    available = ", ".join(sorted(voices)) if voices else "(none)"
    raise SystemExit(f"Set TTS_DEFAULT_VOICE_NAME in .env or pass --voice-name. Available voices: {available}")


def open_player(sample_rate: int) -> subprocess.Popen[bytes]:
    play = shutil.which("play")
    if not play:
        raise SystemExit("SoX 'play' was not found on PATH. Install sox.")
    return subprocess.Popen(
        [
            play,
            "-q",
            "-t",
            "raw",
            "-b",
            "32",
            "-e",
            "floating-point",
            "-c",
            "1",
            "-r",
            str(sample_rate),
            "-",
        ],
        stdin=subprocess.PIPE,
        bufsize=0,
    )


def main() -> int:
    args = parse_args()
    voices_dir = Path(args.voices_dir).expanduser().resolve()
    voices = discover_voices(voices_dir)

    if args.list_voices:
        if not voices:
            print("(no voices found)")
            return 0
        for voice_name in sorted(voices):
            info = voices[voice_name]
            audio_path = info["audio_path"]
            transcript_path = info["transcript_path"]
            print(f"{voice_name}: audio={audio_path} transcript={transcript_path or '(missing)'}")
        return 0

    text = get_text(args)
    if not text:
        raise SystemExit("Text is required.")

    voice_name = resolve_voice_name(args, voices)
    voice_info = voices[voice_name]
    ref_audio = str(voice_info["audio_path"])
    ref_text = voice_info["transcript_text"]
    if not args.x_vector_only and not isinstance(ref_text, str):
        raise SystemExit(f"Voice {voice_name!r} is missing a matching transcript .txt file.")

    model_path = resolve_model_path(args.model_path)
    output_path = Path(args.output).expanduser().resolve() if args.output else None
    device_map = "cuda:0" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if torch.cuda.is_available() else torch.float32

    print(f"Using model: {model_path}", flush=True)
    print(f"Using voice: {voice_name}", flush=True)
    print(f"Using device: {device_map}", flush=True)

    load_start = time.time()
    model = Qwen3TTSModel.from_pretrained(
        model_path,
        device_map=device_map,
        dtype=dtype,
        attn_implementation=args.attn_implementation,
    )
    print(f"Model loaded in {time.time() - load_start:.2f}s", flush=True)

    if args.enable_optimizations:
        opt_start = time.time()
        model.enable_streaming_optimizations(
            decode_window_frames=args.decode_window_frames,
            use_compile=True,
            compile_mode="reduce-overhead",
        )
        print(f"Streaming optimizations enabled in {time.time() - opt_start:.2f}s", flush=True)

    prompt_start = time.time()
    voice_clone_prompt = model.create_voice_clone_prompt(
        ref_audio=ref_audio,
        ref_text=ref_text,
        x_vector_only_mode=args.x_vector_only,
    )
    print(f"Voice clone prompt built in {time.time() - prompt_start:.2f}s", flush=True)

    player: subprocess.Popen[bytes] | None = None
    chunks: list[np.ndarray] = []
    sample_rate = 24000
    first_chunk_at: float | None = None
    stream_start = time.time()

    try:
        for chunk, chunk_sample_rate in model.stream_generate_voice_clone(
            text=text,
            language=args.language,
            voice_clone_prompt=voice_clone_prompt,
            emit_every_frames=args.emit_every_frames,
            decode_window_frames=args.decode_window_frames,
            overlap_samples=512,
            first_chunk_emit_every=args.first_chunk_emit_every,
            first_chunk_decode_window=args.first_chunk_decode_window,
            first_chunk_frames=args.first_chunk_frames,
            repetition_penalty=args.repetition_penalty,
        ):
            chunk = np.asarray(chunk, dtype=np.float32)
            sample_rate = int(chunk_sample_rate)
            if first_chunk_at is None:
                first_chunk_at = time.time() - stream_start
                print(f"First chunk in {first_chunk_at:.2f}s ({len(chunk)} samples)", flush=True)
            if player is None:
                player = open_player(sample_rate)
            if player.stdin:
                player.stdin.write(np.ascontiguousarray(chunk).tobytes())
                player.stdin.flush()
            if output_path is not None:
                chunks.append(chunk.copy())
    finally:
        if player and player.stdin:
            player.stdin.close()
        if player:
            return_code = player.wait()
            if return_code != 0:
                raise SystemExit(f"play exited with status {return_code}")

    total_time = time.time() - stream_start
    print(f"Streaming finished in {total_time:.2f}s", flush=True)

    if output_path is not None:
        if not chunks:
            raise SystemExit("No audio chunks were produced.")
        final_audio = np.concatenate(chunks)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(output_path, final_audio, sample_rate)
        print(f"Saved WAV to {output_path}", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
