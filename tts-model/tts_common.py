"""Shared helpers for Qwen3-TTS scripts in this repo."""

from __future__ import annotations

import base64
import io
from pathlib import Path

import gradio as gr
import httpx
import numpy as np
import soundfile as sf

TTS_MODEL_DIR = Path(__file__).resolve().parent

SUPPORTED_LANGUAGES = [
    "Auto",
    "Chinese",
    "English",
    "Japanese",
    "Korean",
    "German",
    "French",
    "Russian",
    "Portuguese",
    "Spanish",
    "Italian",
]

TASK_TYPES = ["CustomVoice", "VoiceDesign", "Base"]
PCM_SAMPLE_RATE = 24000
DEFAULT_API_BASE = "http://localhost:8091"
DEFAULT_STAGE_CONFIG = str(TTS_MODEL_DIR / "qwen3_tts.yaml")
DEFAULT_TTS_PORT = 8091
DEFAULT_GRADIO_PORT = 7860
DEFAULT_VOICE = "vivian"
DEFAULT_SAMPLE_TEXT = "Blue team are setting up around Baron. This fight could decide the game."


def fetch_voices(api_base: str) -> list[str]:
    """Fetch available voices from the server."""
    try:
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(
                f"{api_base}/v1/audio/voices",
                headers={"Authorization": "Bearer EMPTY"},
            )
        if resp.status_code == 200:
            data = resp.json()
            voices = data.get("voices") or []
            if voices:
                return sorted({voice for voice in voices if isinstance(voice, str)})
    except Exception:
        pass
    return ["vivian", "ryan"]


def add_voice_fields(payload: dict, voice: str | None) -> None:
    """Set both common field names used by different Qwen3-TTS examples."""
    if not voice:
        return
    payload["speaker"] = voice
    payload["voice"] = voice


def encode_audio_to_base64(audio_data: tuple[int, np.ndarray]) -> str:
    """Encode Gradio audio input (sample_rate, numpy_array) to a data URL."""
    sample_rate, audio_np = audio_data

    if audio_np.dtype != np.int16:
        if audio_np.dtype in (np.float32, np.float64):
            audio_np = np.clip(audio_np, -1.0, 1.0)
            audio_np = (audio_np * 32767).astype(np.int16)
        else:
            audio_np = audio_np.astype(np.int16)

    buf = io.BytesIO()
    sf.write(buf, audio_np, sample_rate, format="WAV")
    wav_b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:audio/wav;base64,{wav_b64}"


def build_payload(
    text: str,
    task_type: str,
    voice: str,
    language: str,
    instructions: str,
    ref_audio: tuple[int, np.ndarray] | None,
    ref_audio_url: str,
    ref_text: str,
    x_vector_only: bool,
    response_format: str = "pcm",
    speed: float = 1.0,
    stream: bool = True,
) -> dict:
    """Build a request body for `/v1/audio/speech`."""
    if not text or not text.strip():
        raise gr.Error("Please enter text to synthesize.")

    payload: dict = {
        "input": text.strip(),
        "response_format": "pcm" if stream else response_format,
        "stream": stream,
    }
    if not stream:
        payload["speed"] = speed

    if task_type:
        payload["task_type"] = task_type
    if language:
        payload["language"] = language

    if task_type == "CustomVoice":
        add_voice_fields(payload, voice)
        if instructions and instructions.strip():
            payload["instructions"] = instructions.strip()

    elif task_type == "VoiceDesign":
        if not instructions or not instructions.strip():
            raise gr.Error("VoiceDesign requires a style instruction.")
        payload["instructions"] = instructions.strip()

    elif task_type == "Base":
        ref_audio_url_stripped = ref_audio_url.strip() if ref_audio_url else ""
        if ref_audio_url_stripped:
            payload["ref_audio"] = ref_audio_url_stripped
        elif ref_audio is not None:
            payload["ref_audio"] = encode_audio_to_base64(ref_audio)
        else:
            raise gr.Error("Base voice cloning requires a reference clip.")
        if ref_text and ref_text.strip():
            payload["ref_text"] = ref_text.strip()
        if x_vector_only:
            payload["x_vector_only_mode"] = True

    return payload


def stream_pcm_chunks(api_base: str, payload: dict):
    """Yield raw PCM chunks while preserving odd-byte network boundaries."""
    leftover = b""
    with httpx.Client(timeout=300.0) as client:
        with client.stream(
            "POST",
            f"{api_base}/v1/audio/speech",
            json=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer EMPTY",
            },
        ) as resp:
            if resp.status_code != 200:
                resp.read()
                raise gr.Error(f"Server error ({resp.status_code}): {resp.text}")
            for chunk in resp.iter_bytes():
                if not chunk:
                    continue
                raw = leftover + chunk
                usable = len(raw) - (len(raw) % 2)
                leftover = raw[usable:]
                if usable == 0:
                    continue
                yield np.frombuffer(raw[:usable], dtype=np.int16).copy()


def add_common_args(parser):
    """Add CLI arguments shared by local demo scripts."""
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"Base URL for the vLLM-Omni API server (default: {DEFAULT_API_BASE}).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Host or IP for the local Gradio server.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_GRADIO_PORT,
        help=f"Port for the local Gradio server (default: {DEFAULT_GRADIO_PORT}).",
    )
    parser.add_argument(
        "--share",
        action="store_true",
        help="Expose the Gradio demo using Gradio share mode.",
    )
    return parser
