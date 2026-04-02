#!/usr/bin/env python3
"""Compute a Qwen3-TTS speaker embedding from a reference WAV on CPU."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

os.environ.setdefault("VLLM_LOGGING_LEVEL", "ERROR")

import librosa
import numpy as np
import soundfile as sf
import torch
from safetensors.torch import load_file
from vllm_omni.model_executor.models.qwen3_tts.configuration_qwen3_tts import (
    Qwen3TTSSpeakerEncoderConfig,
)
from vllm_omni.model_executor.models.qwen3_tts.qwen3_tts_talker import (
    Qwen3TTSSpeakerEncoder,
    mel_spectrogram,
)


def parse_args() -> argparse.Namespace:
    script_dir = Path(__file__).resolve().parent
    repo_root = script_dir.parent
    default_model_dir = (
        repo_root
        / ".hf-cache"
        / "hub"
        / "models--Qwen--Qwen3-TTS-12Hz-0.6B-Base"
        / "snapshots"
    )

    parser = argparse.ArgumentParser(description="Compute a Qwen3-TTS speaker embedding.")
    parser.add_argument("audio_file", help="Path to the reference audio file.")
    parser.add_argument(
        "--model-dir",
        default=str(default_model_dir),
        help="Path to the downloaded Qwen3-TTS-0.6B-Base snapshot dir or snapshots dir.",
    )
    return parser.parse_args()


def resolve_model_dir(model_dir_arg: str) -> Path:
    model_dir = Path(model_dir_arg).expanduser().resolve()
    if (model_dir / "config.json").is_file() and (model_dir / "model.safetensors").is_file():
        return model_dir

    refs_main = model_dir.parent / "refs" / "main"
    if model_dir.name == "snapshots" and refs_main.is_file():
        ref = refs_main.read_text().strip()
        candidate = model_dir / ref
        if (candidate / "config.json").is_file() and (candidate / "model.safetensors").is_file():
            return candidate

    snapshots = sorted([p for p in model_dir.iterdir() if p.is_dir()], reverse=True) if model_dir.is_dir() else []
    for candidate in snapshots:
        if (candidate / "config.json").is_file() and (candidate / "model.safetensors").is_file():
            return candidate

    raise SystemExit(f"Could not find config.json and model.safetensors under: {model_dir}")


def load_audio(audio_path: Path, target_sr: int) -> np.ndarray:
    wav, sr = sf.read(str(audio_path))
    if wav.ndim > 1:
        wav = wav.mean(axis=1)
    wav = np.asarray(wav, dtype=np.float32)
    if sr != target_sr:
        wav = librosa.resample(y=wav, orig_sr=int(sr), target_sr=int(target_sr))
    return wav


def main() -> int:
    args = parse_args()
    model_dir = resolve_model_dir(args.model_dir)
    audio_path = Path(args.audio_file).expanduser().resolve()

    if not audio_path.is_file():
        raise SystemExit(f"Missing audio file: {audio_path}")

    config = json.loads((model_dir / "config.json").read_text())
    speaker_config = Qwen3TTSSpeakerEncoderConfig(**config["speaker_encoder_config"])

    model = Qwen3TTSSpeakerEncoder(speaker_config)
    state = load_file(str(model_dir / "model.safetensors"), device="cpu")
    speaker_state = {
        key.removeprefix("speaker_encoder."): value.float()
        for key, value in state.items()
        if key.startswith("speaker_encoder.")
    }
    model.load_state_dict(speaker_state, strict=True)
    model.eval()

    wav = load_audio(audio_path, int(speaker_config.sample_rate))
    mel = mel_spectrogram(
        torch.from_numpy(wav).unsqueeze(0),
        n_fft=1024,
        num_mels=128,
        sampling_rate=int(speaker_config.sample_rate),
        hop_size=256,
        win_size=1024,
        fmin=0,
        fmax=12000,
    ).transpose(1, 2)

    with torch.inference_mode():
        embedding = model(mel.float())[0].cpu().tolist()

    print(json.dumps(embedding))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
