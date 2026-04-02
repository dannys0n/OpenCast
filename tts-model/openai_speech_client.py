#!/usr/bin/env python3

"""OpenAI-compatible client for Qwen3-TTS via `/v1/audio/speech`."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencast_bootstrap import maybe_reexec_with_repo_venv

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)

import httpx

from tts_common import DEFAULT_API_BASE, DEFAULT_SAMPLE_TEXT, DEFAULT_VOICE, add_voice_fields

DEFAULT_API_KEY = "EMPTY"
DEFAULT_MODEL = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"


def encode_audio_to_base64(audio_path: str) -> str:
    """Encode a local audio file to a base64 data URL."""
    path = Path(audio_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    mime_map = {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
        ".aac": "audio/aac",
        ".webm": "audio/webm",
    }
    mime_type = mime_map.get(path.suffix.lower(), "audio/wav")
    audio_b64 = base64.b64encode(path.read_bytes()).decode("utf-8")
    return f"data:{mime_type};base64,{audio_b64}"


def build_payload(args: argparse.Namespace) -> dict:
    payload: dict = {
        "model": args.model,
        "input": args.text,
        "response_format": args.response_format,
    }

    if args.task_type:
        payload["task_type"] = args.task_type
    if args.language:
        payload["language"] = args.language
    if args.instructions:
        payload["instructions"] = args.instructions
    if args.max_new_tokens:
        payload["max_new_tokens"] = args.max_new_tokens
    if args.stream:
        payload["stream"] = True

    task_type = args.task_type or "CustomVoice"
    if task_type == "CustomVoice":
        add_voice_fields(payload, args.speaker)

    if args.ref_audio:
        if args.ref_audio.startswith(("http://", "https://", "data:")):
            payload["ref_audio"] = args.ref_audio
        else:
            payload["ref_audio"] = encode_audio_to_base64(args.ref_audio)
    if args.ref_text:
        payload["ref_text"] = args.ref_text
    if args.x_vector_only:
        payload["x_vector_only_mode"] = True
    if args.speaker_embedding:
        with open(args.speaker_embedding, encoding="utf-8") as handle:
            payload["speaker_embedding"] = json.load(handle)
    return payload


def run_tts_generation(args: argparse.Namespace) -> None:
    """Send a TTS request and save the returned audio bytes."""
    if args.stream and args.response_format != "pcm":
        raise SystemExit("--stream requires --response-format pcm")

    payload = build_payload(args)
    api_url = f"{args.api_base}/v1/audio/speech"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {args.api_key}",
    }

    output_path = Path(args.output or ("tts_stream_output.pcm" if args.stream else "tts_output.wav"))
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Model: {args.model}")
    print(f"Task type: {args.task_type or 'CustomVoice'}")
    print(f"Text: {args.text}")
    print(f"Speaker: {args.speaker}")
    print(f"Streaming: {args.stream}")
    print(f"Saving to: {output_path}")

    if args.stream:
        total_bytes = 0
        with httpx.Client(timeout=300.0) as client:
            with client.stream("POST", api_url, json=payload, headers=headers) as response:
                if response.status_code != 200:
                    raise SystemExit(f"Error {response.status_code}: {response.text}")
                with output_path.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        if not chunk:
                            continue
                        handle.write(chunk)
                        total_bytes += len(chunk)
        print(f"Streamed {total_bytes} bytes to: {output_path}")
        return

    with httpx.Client(timeout=300.0) as client:
        response = client.post(api_url, json=payload, headers=headers)

    if response.status_code != 200:
        raise SystemExit(f"Error {response.status_code}: {response.text}")

    try:
        text = response.content.decode("utf-8")
        if text.startswith('{"error"'):
            raise SystemExit(f"Error: {text}")
    except UnicodeDecodeError:
        pass

    output_path.write_bytes(response.content)
    print(f"Audio saved to: {output_path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI-compatible client for Qwen3-TTS via /v1/audio/speech",
    )
    parser.add_argument("--api-base", default=DEFAULT_API_BASE, help=f"API base URL (default: {DEFAULT_API_BASE})")
    parser.add_argument("--api-key", default=DEFAULT_API_KEY, help="API key (default: EMPTY)")
    parser.add_argument("--model", "-m", default=DEFAULT_MODEL, help="Model name or path")
    parser.add_argument(
        "--task-type",
        "-t",
        default=None,
        choices=["CustomVoice", "VoiceDesign", "Base"],
        help="TTS task type (default: CustomVoice)",
    )
    parser.add_argument("--text", default=DEFAULT_SAMPLE_TEXT, help="Text to synthesize")
    parser.add_argument("--speaker", default=DEFAULT_VOICE, help="Speaker name for CustomVoice requests")
    parser.add_argument("--language", default=None, help="Language: Auto, Chinese, English, etc.")
    parser.add_argument("--instructions", default=None, help="Voice style or emotion instructions")
    parser.add_argument("--ref-audio", default=None, help="Reference audio path, URL, or data URL for Base cloning")
    parser.add_argument("--ref-text", default=None, help="Reference transcript for Base cloning")
    parser.add_argument("--x-vector-only", action="store_true", help="Use x-vector only mode for voice cloning")
    parser.add_argument("--speaker-embedding", default=None, help="Path to a JSON speaker embedding")
    parser.add_argument("--max-new-tokens", type=int, default=None, help="Maximum new tokens to generate")
    parser.add_argument(
        "--response-format",
        default="wav",
        choices=["wav", "mp3", "flac", "pcm", "aac", "opus"],
        help="Audio output format",
    )
    parser.add_argument("--stream", action="store_true", help="Request chunked PCM streaming output")
    parser.add_argument("--output", "-o", default=None, help="Output audio file path")
    return parser.parse_args()


if __name__ == "__main__":
    run_tts_generation(parse_args())
