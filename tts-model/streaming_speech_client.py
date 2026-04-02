#!/usr/bin/env python3

"""WebSocket client for incremental text-input Qwen3-TTS sessions."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencast_bootstrap import maybe_reexec_with_repo_venv
from opencast_audio import PCMStreamPlayer

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)

try:
    import websockets
except ImportError as exc:  # pragma: no cover - surfaced at runtime for missing optional dep
    raise SystemExit("Please install websockets in the local environment.") from exc

from tts_common import DEFAULT_SAMPLE_TEXT, DEFAULT_VOICE

DEFAULT_WS_URL = "ws://localhost:8091/v1/audio/speech/stream"


async def stream_tts(
    url: str,
    text: str,
    config: dict,
    output_dir: str,
    play_live: bool = False,
    audio_player: str = "auto",
    simulate_stt: bool = False,
    stt_delay: float = 0.1,
) -> None:
    """Connect to the streaming endpoint and stream audio locally."""
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)
    player = PCMStreamPlayer(preferred=audio_player) if play_live else None

    try:
        async with websockets.connect(url) as ws:
            config_msg = {"type": "session.config", **config}
            await ws.send(json.dumps(config_msg))
            print(f"Sent session config: {config}")
            if player is not None:
                print(f"Live playback enabled via {player.name}")

            async def send_text() -> None:
                if simulate_stt:
                    words = text.split(" ")
                    for index, word in enumerate(words):
                        chunk = word + (" " if index < len(words) - 1 else "")
                        await ws.send(json.dumps({"type": "input.text", "text": chunk}))
                        print(f"  Sent: {chunk!r}")
                        await asyncio.sleep(stt_delay)
                else:
                    await ws.send(json.dumps({"type": "input.text", "text": text}))
                    print(f"Sent full text: {text!r}")

                await ws.send(json.dumps({"type": "input.done"}))
                print("Sent input.done")

            sender_task = asyncio.create_task(send_text())
            response_format = config.get("response_format", "wav")
            current_sentence_index = 0
            current_chunks: list[bytes] = []

            try:
                while True:
                    message = await ws.recv()
                    if isinstance(message, bytes):
                        current_chunks.append(message)
                        if player is not None:
                            player.write(message)
                        print(f"  Received audio chunk for sentence {current_sentence_index}: {len(message)} bytes")
                        continue

                    msg = json.loads(message)
                    msg_type = msg.get("type")

                    if msg_type == "audio.start":
                        current_sentence_index = msg["sentence_index"]
                        current_chunks = []
                        print(f"  [sentence {msg['sentence_index']}] Generating: {msg['sentence_text']!r}")
                    elif msg_type == "audio.done":
                        filename = output_path / f"sentence_{msg['sentence_index']:03d}.{response_format}"
                        filename.write_bytes(b"".join(current_chunks))
                        print(
                            f"  [sentence {msg['sentence_index']}] Done"
                            f" bytes={msg.get('total_bytes', len(b''.join(current_chunks)))}"
                            f" error={msg.get('error', False)}"
                            f" -> {filename}"
                        )
                        current_chunks = []
                    elif msg_type == "session.done":
                        print(f"\nSession complete: {msg['total_sentences']} sentence(s) generated")
                        break
                    elif msg_type == "error":
                        raise RuntimeError(msg["message"])
                    else:
                        print(f"  Unknown message: {msg}")
            finally:
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass
    finally:
        if player is not None:
            player.close()

    print(f"\nAudio files saved to: {output_path}/")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Streaming text-input TTS client")
    parser.add_argument("--url", default=DEFAULT_WS_URL, help=f"WebSocket endpoint URL (default: {DEFAULT_WS_URL})")
    parser.add_argument("--text", default=DEFAULT_SAMPLE_TEXT, help="Text to synthesize")
    parser.add_argument("--output-dir", default=".cache/opencast/ws-output", help="Directory for per-sentence outputs")
    parser.add_argument("--model", default=None, help="Optional model override")
    parser.add_argument("--speaker", default=DEFAULT_VOICE, help="Speaker name for CustomVoice sessions")
    parser.add_argument(
        "--task-type",
        default="CustomVoice",
        choices=["CustomVoice", "VoiceDesign", "Base"],
        help="TTS task type",
    )
    parser.add_argument("--language", default="Auto", help="Language")
    parser.add_argument("--instructions", default=None, help="Voice style instructions")
    parser.add_argument(
        "--response-format",
        default="pcm",
        choices=["wav", "pcm", "flac", "mp3", "aac", "opus"],
        help="Audio format",
    )
    parser.add_argument("--stream-audio", action="store_true", help="Receive one or more PCM chunks per sentence")
    parser.add_argument("--play-live", action="store_true", help="Play PCM chunks locally as they arrive")
    parser.add_argument(
        "--audio-player",
        default="auto",
        choices=["auto", "pw-play", "aplay", "ffplay"],
        help="Preferred local player for live PCM playback",
    )
    parser.add_argument("--speed", type=float, default=1.0, help="Playback speed (not supported with stream_audio)")
    parser.add_argument("--max-new-tokens", type=int, default=None, help="Maximum tokens")
    parser.add_argument("--ref-audio", default=None, help="Reference audio path or URL for Base cloning")
    parser.add_argument("--ref-text", default=None, help="Reference transcript for Base cloning")
    parser.add_argument("--x-vector-only-mode", action="store_true", default=False, help="Speaker embedding only mode")
    parser.add_argument("--simulate-stt", action="store_true", help="Send text word-by-word to simulate STT")
    parser.add_argument("--stt-delay", type=float, default=0.1, help="Delay between words when simulating STT")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> dict:
    if args.stream_audio and args.response_format != "pcm":
        raise SystemExit("--stream-audio requires --response-format pcm")
    if args.stream_audio and args.speed != 1.0:
        raise SystemExit("--speed is not supported when --stream-audio is enabled")
    if args.play_live and not args.stream_audio:
        raise SystemExit("--play-live requires --stream-audio")
    if args.play_live and args.response_format != "pcm":
        raise SystemExit("--play-live requires --response-format pcm")

    config: dict = {
        "speaker": args.speaker,
        "voice": args.speaker,
        "task_type": args.task_type,
        "language": args.language,
        "response_format": args.response_format,
    }

    for key in ["model", "instructions", "max_new_tokens", "ref_audio", "ref_text"]:
        value = getattr(args, key)
        if value is not None:
            config[key] = value
    if args.stream_audio:
        config["stream_audio"] = True
    if args.x_vector_only_mode:
        config["x_vector_only_mode"] = True
    if not args.stream_audio:
        config["speed"] = args.speed
    return config


def main() -> None:
    args = parse_args()
    asyncio.run(
        stream_tts(
            url=args.url,
            text=args.text,
            config=build_config(args),
            output_dir=args.output_dir,
            play_live=args.play_live,
            audio_player=args.audio_player,
            simulate_stt=args.simulate_stt,
            stt_delay=args.stt_delay,
        )
    )


if __name__ == "__main__":
    main()
