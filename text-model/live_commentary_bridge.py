#!/usr/bin/env python3

"""Generate local commentary and stream it into the Qwen3-TTS WebSocket API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencast_bootstrap import maybe_reexec_with_repo_venv
from opencast_audio import PCMStreamPlayer

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)

TEXT_MODEL_DIR = Path(__file__).resolve().parent
TTS_MODEL_DIR = REPO_ROOT / "tts-model"

for extra_path in (TEXT_MODEL_DIR, TTS_MODEL_DIR):
    extra_path_str = str(extra_path)
    if extra_path_str not in sys.path:
        sys.path.insert(0, extra_path_str)

from commentary_model_client import (  # noqa: E402
    DEFAULT_COMMENTARY_MODEL,
    DEFAULT_DMR_BASE_URL,
    DEFAULT_LINES_PER_UPDATE,
    DEFAULT_MAX_TOKENS,
    DEFAULT_PROMPT_FILE,
    DEFAULT_TEMPERATURE,
    DEFAULT_TOP_P,
    CommentaryGenerationConfig,
    DockerModelRunnerClient,
)
from tts_common import DEFAULT_VOICE  # noqa: E402

try:
    import websockets
except ImportError as exc:  # pragma: no cover - surfaced at runtime for missing optional dep
    raise SystemExit("Please install websockets in the local environment.") from exc

DEFAULT_WS_URL = os.environ.get(
    "OPENCAST_TTS_WS_URL",
    "ws://localhost:8091/v1/audio/speech/stream",
)
DEFAULT_OUTPUT_ROOT = REPO_ROOT / ".cache/opencast/commentary-bridge"
DEFAULT_TTS_MODEL = os.environ.get(
    "OPENCAST_COMMENTARY_TTS_MODEL",
    "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
)
DEFAULT_TTS_SPEAKER = os.environ.get("OPENCAST_COMMENTARY_TTS_SPEAKER", DEFAULT_VOICE)
DEFAULT_TTS_TASK_TYPE = os.environ.get("OPENCAST_COMMENTARY_TTS_TASK_TYPE", "CustomVoice")
DEFAULT_TTS_LANGUAGE = os.environ.get("OPENCAST_COMMENTARY_TTS_LANGUAGE", "English")
DEFAULT_SEND_DELAY = float(os.environ.get("OPENCAST_COMMENTARY_TTS_SEND_DELAY", "0.05"))
DEFAULT_PLAY_LIVE = os.environ.get("OPENCAST_COMMENTARY_PLAY_LIVE", "1").lower() not in {"0", "false", "no"}
DEFAULT_AUDIO_PLAYER = os.environ.get("OPENCAST_AUDIO_PLAYER", "auto")


@dataclass
class CommentaryRecord:
    update_index: int
    update_text: str
    lines: list[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Bridge local match-state notes into Qwen3-TTS audio.")
    parser.add_argument(
        "--event",
        action="append",
        default=[],
        help="One match-state update. Pass multiple times for multiple updates.",
    )
    parser.add_argument("--input-file", default=None, help="Text file with one match-state update per line")
    parser.add_argument("--context", default=None, help="Optional persistent casting context")
    parser.add_argument("--output-dir", default=None, help="Directory for audio outputs and transcript")
    parser.add_argument("--transcript-file", default=None, help="Optional transcript output path")
    parser.add_argument("--commentary-base-url", default=DEFAULT_DMR_BASE_URL, help="DMR API base URL")
    parser.add_argument("--commentary-model", default=DEFAULT_COMMENTARY_MODEL, help="DMR model ID")
    parser.add_argument("--commentary-prompt-file", default=DEFAULT_PROMPT_FILE, help="Commentary system prompt")
    parser.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE, help="Commentary temperature")
    parser.add_argument("--top-p", type=float, default=DEFAULT_TOP_P, help="Commentary top-p")
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS, help="Commentary max tokens")
    parser.add_argument(
        "--lines-per-update",
        type=int,
        default=DEFAULT_LINES_PER_UPDATE,
        help="Number of short commentary lines to request per update",
    )
    parser.add_argument("--ws-url", default=DEFAULT_WS_URL, help="TTS streaming WebSocket URL")
    parser.add_argument("--tts-model", default=DEFAULT_TTS_MODEL, help="Optional TTS model override")
    parser.add_argument("--speaker", default=DEFAULT_TTS_SPEAKER, help="TTS speaker")
    parser.add_argument(
        "--task-type",
        default=DEFAULT_TTS_TASK_TYPE,
        choices=["CustomVoice", "VoiceDesign", "Base"],
        help="TTS task type",
    )
    parser.add_argument("--language", default=DEFAULT_TTS_LANGUAGE, help="TTS language")
    parser.add_argument("--instructions", default=None, help="Optional TTS voice instructions")
    parser.add_argument(
        "--response-format",
        default="pcm",
        choices=["wav", "pcm", "flac", "mp3", "aac", "opus"],
        help="Audio format for sentence files",
    )
    parser.add_argument("--tts-send-delay", type=float, default=DEFAULT_SEND_DELAY, help="Delay between streamed lines")
    parser.add_argument(
        "--play-live",
        action=argparse.BooleanOptionalAction,
        default=DEFAULT_PLAY_LIVE,
        help="Play PCM chunks locally as they arrive",
    )
    parser.add_argument(
        "--audio-player",
        default=DEFAULT_AUDIO_PLAYER,
        choices=["auto", "pw-play", "aplay", "ffplay"],
        help="Preferred local player for live PCM playback",
    )
    return parser.parse_args()


def load_updates(args: argparse.Namespace) -> list[str]:
    updates = [item.strip() for item in args.event if item and item.strip()]

    if args.input_file:
        for raw_line in Path(args.input_file).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                updates.append(line)

    if not updates and not sys.stdin.isatty():
        for raw_line in sys.stdin.read().splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                updates.append(line)

    if not updates:
        raise SystemExit("Provide one or more updates with --event, --input-file, or stdin.")
    return updates


def resolve_output_dir(raw_output_dir: str | None) -> Path:
    if raw_output_dir:
        output_dir = Path(raw_output_dir)
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        output_dir = DEFAULT_OUTPUT_ROOT / timestamp
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_tts_config(args: argparse.Namespace) -> dict:
    config: dict = {
        "task_type": args.task_type,
        "language": args.language,
        "response_format": args.response_format,
        "stream_audio": True,
    }
    if args.tts_model:
        config["model"] = args.tts_model
    if args.speaker:
        config["speaker"] = args.speaker
        config["voice"] = args.speaker
    if args.instructions:
        config["instructions"] = args.instructions
    return config


async def send_commentary(
    ws,
    updates: list[str],
    client: DockerModelRunnerClient,
    context: str | None,
    send_delay: float,
) -> list[CommentaryRecord]:
    records: list[CommentaryRecord] = []
    for update_index, update_text in enumerate(updates, start=1):
        lines = await asyncio.to_thread(
            client.generate_commentary,
            match_state=update_text,
            context=context,
        )
        records.append(CommentaryRecord(update_index=update_index, update_text=update_text, lines=lines))
        print(f"[update {update_index:03d}] {update_text}")
        for line_index, line in enumerate(lines, start=1):
            print(f"  [commentary {update_index:03d}.{line_index:02d}] {line}")
            await ws.send(json.dumps({"type": "input.text", "text": f"{line} "}))
            if send_delay > 0:
                await asyncio.sleep(send_delay)

    await ws.send(json.dumps({"type": "input.done"}))
    print("Sent input.done")
    return records


async def receive_audio(ws, output_dir: Path, response_format: str, player: PCMStreamPlayer | None) -> int:
    current_chunks: list[bytes] = []
    written_files = 0

    while True:
        message = await ws.recv()
        if isinstance(message, bytes):
            current_chunks.append(message)
            if player is not None:
                player.write(message)
            continue

        payload = json.loads(message)
        msg_type = payload.get("type")

        if msg_type == "audio.start":
            current_chunks = []
            print(f"  [tts {payload['sentence_index']:03d}] Generating: {payload['sentence_text']!r}")
        elif msg_type == "audio.done":
            filename = output_dir / f"sentence_{payload['sentence_index']:03d}.{response_format}"
            filename.write_bytes(b"".join(current_chunks))
            written_files += 1
            print(f"  [tts {payload['sentence_index']:03d}] Saved {filename}")
            current_chunks = []
        elif msg_type == "session.done":
            print(f"Session complete: {payload['total_sentences']} sentence(s) generated")
            return written_files
        elif msg_type == "error":
            raise RuntimeError(payload["message"])
        else:
            print(f"  Unknown message: {payload}")


def write_transcript(records: list[CommentaryRecord], transcript_path: Path) -> None:
    lines: list[str] = []
    for record in records:
        lines.extend(
            [
                f"[update {record.update_index:03d}]",
                record.update_text,
            ]
        )
        for line_index, line in enumerate(record.lines, start=1):
            lines.extend(
                [
                    f"[commentary {record.update_index:03d}.{line_index:02d}]",
                    line,
                ]
            )
        lines.append("")
    transcript_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


async def run_bridge(args: argparse.Namespace, output_dir: Path) -> tuple[list[CommentaryRecord], int, str | None]:
    updates = load_updates(args)
    commentary_client = DockerModelRunnerClient(
        CommentaryGenerationConfig(
            base_url=args.commentary_base_url,
            model=args.commentary_model,
            prompt_file=args.commentary_prompt_file,
            temperature=args.temperature,
            top_p=args.top_p,
            max_tokens=args.max_tokens,
            lines_per_update=args.lines_per_update,
        )
    )
    commentary_client.ensure_model_available()
    tts_config = build_tts_config(args)
    player: PCMStreamPlayer | None = None

    if args.play_live:
        if args.response_format != "pcm":
            raise SystemExit("--play-live requires --response-format pcm")
        player = PCMStreamPlayer(preferred=args.audio_player)

    try:
        async with websockets.connect(args.ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "session.config", **tts_config}))
            print(f"Sent session config: {tts_config}")
            if player is not None:
                print(f"Live playback enabled via {player.name}")
            received_count, records = await asyncio.gather(
                receive_audio(ws, output_dir, args.response_format, player),
                send_commentary(
                    ws=ws,
                    updates=updates,
                    client=commentary_client,
                    context=args.context,
                    send_delay=args.tts_send_delay,
                ),
            )
    finally:
        if player is not None:
            player.close()
    return records, received_count, player.name if player is not None else None


def main() -> None:
    args = parse_args()
    output_dir = resolve_output_dir(args.output_dir)
    transcript_path = Path(args.transcript_file) if args.transcript_file else output_dir / "commentary_transcript.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)

    records, written_files, player_name = asyncio.run(run_bridge(args, output_dir))
    write_transcript(records, transcript_path)

    print()
    print(f"Commentary transcript: {transcript_path}")
    print(f"Audio output dir     : {output_dir}")
    print(f"Sentence files       : {written_files}")
    print(f"Live playback        : {player_name or 'disabled'}")


if __name__ == "__main__":
    main()
