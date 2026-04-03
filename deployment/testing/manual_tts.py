#!/usr/bin/env python3
"""Manually send an array of {voice, text} items to the TTS server."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
DEFAULT_MANIFEST = REPO_ROOT / "tts-io" / "voices" / "generated" / "voices.json"
DEFAULT_WS_URL = "ws://localhost:8091/v1/audio/speech/stream"
DEFAULT_LANGUAGE = "English"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Speak a JSON array of {voice, text} requests through Qwen3-TTS."
    )
    parser.add_argument(
        "--requests-json",
        default=None,
        help='Inline JSON array, e.g. \'[{"voice":"june","text":"Hello world"}]\'',
    )
    parser.add_argument(
        "--requests-file",
        default=None,
        help="Path to a JSON file containing an array of {voice, text} items.",
    )
    parser.add_argument(
        "--manifest-file",
        default=str(DEFAULT_MANIFEST),
        help=f"Voice manifest path (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_WS_URL,
        help=f"TTS WebSocket endpoint (default: {DEFAULT_WS_URL})",
    )
    parser.add_argument(
        "--language",
        default=DEFAULT_LANGUAGE,
        help=f"TTS language (default: {DEFAULT_LANGUAGE})",
    )
    return parser.parse_args()


def load_requests(args: argparse.Namespace) -> list[dict]:
    raw_requests: str | None = None
    if args.requests_json:
        raw_requests = args.requests_json
    elif args.requests_file:
        raw_requests = Path(args.requests_file).expanduser().read_text(encoding="utf-8")
    elif not sys.stdin.isatty():
        raw_requests = sys.stdin.read()

    if not raw_requests:
        raise SystemExit(
            "Provide requests with --requests-json, --requests-file, or stdin.\n"
            'Example: --requests-json \'[{"voice":"june","text":"Blue team take Baron."}]\''
        )

    try:
        payload = json.loads(raw_requests)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON input: {exc}") from exc

    if not isinstance(payload, list) or not payload:
        raise SystemExit("Requests must be a non-empty JSON array.")

    validated: list[dict] = []
    for index, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise SystemExit(f"Request {index} must be an object.")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            raise SystemExit(f"Request {index} is missing a non-empty 'text' string.")
        voice = item.get("voice")
        if voice is not None and (not isinstance(voice, str) or not voice.strip()):
            raise SystemExit(f"Request {index} has an invalid 'voice' value.")
        validated.append({"voice": voice.strip() if isinstance(voice, str) else None, "text": text.strip()})
    return validated


def load_manifest(path_str: str) -> tuple[dict[str, dict], str]:
    manifest_path = Path(path_str).expanduser().resolve()
    if not manifest_path.is_file():
        raise SystemExit(f"Missing voice manifest: {manifest_path}\nRun: sh tts-io/add_custom_voice.sh")

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid voice manifest JSON: {manifest_path}") from exc

    voices = payload.get("voices")
    default_voice_name = payload.get("default_voice_name")
    if not isinstance(voices, list) or not voices:
        raise SystemExit(f"Voice manifest has no voices: {manifest_path}")
    if not isinstance(default_voice_name, str) or not default_voice_name:
        raise SystemExit(f"Voice manifest is missing default_voice_name: {manifest_path}")

    voice_map: dict[str, dict] = {}
    for item in voices:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        embedding_file = item.get("embedding_file")
        if isinstance(name, str) and name and isinstance(embedding_file, str) and embedding_file:
            voice_map[name] = item

    if default_voice_name not in voice_map:
        raise SystemExit(f"Default voice {default_voice_name!r} was not found in: {manifest_path}")

    return voice_map, default_voice_name


def resolve_embedding_file(voice_name: str | None, voice_map: dict[str, dict], default_voice_name: str) -> tuple[str, Path]:
    resolved_voice = voice_name or default_voice_name
    item = voice_map.get(resolved_voice)
    if item is None:
        available = ", ".join(sorted(voice_map))
        raise SystemExit(f"Unknown voice {resolved_voice!r}. Available voices: {available}")

    embedding_file = Path(item["embedding_file"]).expanduser().resolve()
    if not embedding_file.is_file():
        raise SystemExit(f"Missing speaker embedding file: {embedding_file}")

    return resolved_voice, embedding_file


def shorten_text(text: str, limit: int = 72) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def run_request(index: int, total: int, voice_name: str, text: str, embedding_file: Path, args: argparse.Namespace) -> None:
    print(f"[{index}/{total}] voice={voice_name} text={shorten_text(text)!r}", flush=True)
    command = [
        sys.executable,
        str(REPO_ROOT / "tts-io" / "stream_tts.py"),
        "--url",
        args.url,
        "--language",
        args.language,
        "--speaker-embedding-file",
        str(embedding_file),
        text,
    ]
    subprocess.run(command, check=True)


def main() -> int:
    args = parse_args()
    requests = load_requests(args)
    voice_map, default_voice_name = load_manifest(args.manifest_file)

    for index, item in enumerate(requests, start=1):
        voice_name, embedding_file = resolve_embedding_file(item["voice"], voice_map, default_voice_name)
        run_request(index, len(requests), voice_name, item["text"], embedding_file, args)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
