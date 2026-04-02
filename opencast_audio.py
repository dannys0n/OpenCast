"""Helpers for low-latency PCM playback on Linux."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

PCM_SAMPLE_RATE = 24000
PCM_CHANNELS = 1


@dataclass(frozen=True)
class AudioPlayerChoice:
    name: str
    command: list[str]


def resolve_audio_player(
    preferred: str = "auto",
    sample_rate: int = PCM_SAMPLE_RATE,
    channels: int = PCM_CHANNELS,
) -> AudioPlayerChoice:
    """Resolve a local PCM playback command."""
    candidates: list[AudioPlayerChoice] = []

    if preferred in ("auto", "pw-play") and shutil.which("pw-play"):
        candidates.append(
            AudioPlayerChoice(
                name="pw-play",
                command=[
                    "pw-play",
                    "--raw",
                    "--rate",
                    str(sample_rate),
                    "--channels",
                    str(channels),
                    "--format",
                    "s16",
                    "--latency",
                    "40ms",
                    "-",
                ],
            )
        )

    if preferred in ("auto", "aplay") and shutil.which("aplay"):
        candidates.append(
            AudioPlayerChoice(
                name="aplay",
                command=[
                    "aplay",
                    "-q",
                    "-t",
                    "raw",
                    "-f",
                    "S16_LE",
                    "-r",
                    str(sample_rate),
                    "-c",
                    str(channels),
                    "-B",
                    "50000",
                    "-F",
                    "10000",
                    "-",
                ],
            )
        )

    if preferred in ("auto", "ffplay") and shutil.which("ffplay"):
        candidates.append(
            AudioPlayerChoice(
                name="ffplay",
                command=[
                    "ffplay",
                    "-autoexit",
                    "-nodisp",
                    "-loglevel",
                    "error",
                    "-fflags",
                    "nobuffer",
                    "-flags",
                    "low_delay",
                    "-probesize",
                    "32",
                    "-analyzeduration",
                    "0",
                    "-f",
                    "s16le",
                    "-ar",
                    str(sample_rate),
                    "-ac",
                    str(channels),
                    "-i",
                    "pipe:0",
                ],
            )
        )

    if preferred != "auto" and not candidates:
        raise RuntimeError(f"Requested audio player {preferred!r} is not available.")
    if not candidates:
        raise RuntimeError("No supported live audio player found. Install pw-play, aplay, or ffplay.")
    return candidates[0]


class PCMStreamPlayer:
    """Write raw PCM bytes to a low-latency local playback process."""

    def __init__(self, preferred: str = "auto", sample_rate: int = PCM_SAMPLE_RATE, channels: int = PCM_CHANNELS):
        self.choice = resolve_audio_player(preferred=preferred, sample_rate=sample_rate, channels=channels)
        self.process = subprocess.Popen(
            self.choice.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=None,
            bufsize=0,
        )

    @property
    def name(self) -> str:
        return self.choice.name

    def write(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.process.poll() is not None:
            raise RuntimeError(f"Audio player {self.choice.name} exited unexpectedly.")
        assert self.process.stdin is not None
        self.process.stdin.write(chunk)
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
