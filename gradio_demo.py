#!/usr/bin/env python3

"""Compatibility wrapper for the TTS Gradio demo."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

from opencast_bootstrap import maybe_reexec_with_repo_venv

ROOT_DIR = Path(__file__).resolve().parent
TTS_MODEL_DIR = ROOT_DIR / "tts-model"

maybe_reexec_with_repo_venv(__file__, ROOT_DIR)
sys.path.insert(0, str(TTS_MODEL_DIR))

runpy.run_path(str(TTS_MODEL_DIR / "gradio_demo.py"), run_name="__main__")
