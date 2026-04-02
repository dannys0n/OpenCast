#!/usr/bin/env python3
"""Stream text into the local Qwen3-TTS WebSocket endpoint and play audio live.

This repo uses Base + x-vector-only only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import websockets

DEFAULT_WS_URL = "ws://localhost:8091/v1/audio/speech/stream"
DEFAULT_LANGUAGE = "English"
DEFAULT_SAMPLE_RATE = 24000


def shorten_text(text: str, limit: int = 72) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: limit - 3] + "..."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stream text into Qwen3-TTS and play the returned audio."
    )
    parser.add_argument(
        "text",
        nargs="*",
        help="Text to speak. If omitted, text is read from stdin.",
    )
    parser.add_argument(
        "--stdin-chunks",
        action="store_true",
        help="Read stdin incrementally, one line per text chunk.",
    )
    parser.add_argument("--url", default=DEFAULT_WS_URL, help="WebSocket endpoint.")
    parser.add_argument(
        "--speaker-embedding-file",
        required=True,
        help="JSON file containing the precomputed speaker embedding.",
    )
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    return parser.parse_args()


def get_text(args: argparse.Namespace) -> str:
    if args.text:
        return " ".join(args.text).strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide text as an argument or pipe it into stdin.")


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
            "16",
            "-e",
            "signed-integer",
            "-c",
            "1",
            "-r",
            str(sample_rate),
            "-",
        ],
        stdin=subprocess.PIPE,
        bufsize=0,
    )


async def send_text(websocket: websockets.ClientConnection, text: str) -> None:
    await websocket.send(json.dumps({"type": "input.text", "text": text}))
    await websocket.send(json.dumps({"type": "input.done"}))


async def send_stdin_chunks(websocket: websockets.ClientConnection) -> None:
    while True:
        chunk = await asyncio.to_thread(sys.stdin.readline)
        if chunk == "":
            break
        chunk = chunk.rstrip("\r\n")
        if not chunk:
            continue
        await websocket.send(json.dumps({"type": "input.text", "text": chunk}))

    await websocket.send(json.dumps({"type": "input.done"}))


def load_speaker_embedding(path_str: str) -> list[float]:
    path = Path(path_str).expanduser().resolve()
    if not path.is_file():
        raise SystemExit(f"Missing speaker embedding file: {path}")

    payload = json.loads(path.read_text())
    embedding = payload.get("speaker_embedding") if isinstance(payload, dict) else payload

    if not isinstance(embedding, list) or not embedding:
        raise SystemExit(f"Invalid speaker embedding file: {path}")

    return [float(value) for value in embedding]


async def main_async(args: argparse.Namespace) -> int:
    sample_rate = DEFAULT_SAMPLE_RATE
    player = None
    bytes_written = 0
    speaker_embedding = load_speaker_embedding(args.speaker_embedding_file)
    current_sentence_index: int | None = None
    current_sentence_bytes = 0
    saw_first_audio_byte = False
    start_time = time.perf_counter()

    def log_event(message: str) -> None:
        delta_s = time.perf_counter() - start_time
        print(f"[+{delta_s:0.3f}s] {message}", file=sys.stderr, flush=True)

    try:
        async with websockets.connect(args.url, max_size=None) as websocket:
            await websocket.send(
                json.dumps(
                    {
                        "type": "session.config",
                        "task_type": "Base",
                        "language": args.language,
                        "response_format": "pcm",
                        "stream_audio": True,
                        "split_granularity": "sentence",
                        "x_vector_only_mode": True,
                        "speaker_embedding": speaker_embedding,
                    }
                )
            )
            log_event("session configured")

            async def run_sender() -> None:
                if args.stdin_chunks:
                    await send_stdin_chunks(websocket)
                else:
                    text = get_text(args)
                    await send_text(websocket, text)
                log_event("input.done sent")

            send_task = asyncio.create_task(run_sender())

            while True:
                message = await websocket.recv()

                if isinstance(message, bytes):
                    bytes_written += len(message)
                    current_sentence_bytes += len(message)
                    if not saw_first_audio_byte:
                        sentence_label = (
                            str(current_sentence_index)
                            if current_sentence_index is not None
                            else "unknown"
                        )
                        log_event(f"sentence {sentence_label} first audio byte")
                        saw_first_audio_byte = True
                    if player is None:
                        player = open_player(sample_rate)
                    if player.stdin:
                        player.stdin.write(message)
                        player.stdin.flush()
                    continue

                event = json.loads(message)
                event_type = event.get("type")
                if event_type == "audio.start":
                    sample_rate = int(event.get("sample_rate", sample_rate))
                    current_sentence_index = event.get("sentence_index")
                    current_sentence_bytes = 0
                    saw_first_audio_byte = False
                    sentence_text = shorten_text(event.get("sentence_text", ""))
                    log_event(
                        f"sentence {current_sentence_index} start @ {sample_rate} Hz: {sentence_text!r}"
                    )
                elif event_type == "audio.done":
                    log_event(
                        f"sentence {event.get('sentence_index', current_sentence_index)} done "
                        f"({event.get('total_bytes', current_sentence_bytes)} bytes, "
                        f"error={event.get('error', False)})"
                    )
                elif event_type == "session.done":
                    log_event(f"session done ({event.get('total_sentences', 'unknown')} sentences)")
                    break
                elif event_type == "error":
                    raise SystemExit(f"TTS stream error: {event.get('message', event)}")

            await send_task

        if player and player.stdin:
            player.stdin.close()
        if player:
            return_code = player.wait()
            if return_code != 0:
                raise SystemExit(f"play exited with status {return_code}")

    finally:
        if player and player.poll() is None:
            player.terminate()

    if bytes_written == 0:
        raise SystemExit("The server returned no audio bytes.")

    return 0


def main() -> int:
    return asyncio.run(main_async(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
