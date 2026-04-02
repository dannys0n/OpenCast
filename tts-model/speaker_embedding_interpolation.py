#!/usr/bin/env python3

"""Launcher for the upstream-style speaker embedding helper with repo-local helpers."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from opencast_bootstrap import maybe_reexec_with_repo_venv

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)

TTS_MODEL_DIR = Path(__file__).resolve().parent
TEMPLATE_DIR = REPO_ROOT / "templates"

sys.path.insert(0, str(TTS_MODEL_DIR))
sys.path.insert(1, str(TEMPLATE_DIR))

runpy.run_path(str(TEMPLATE_DIR / "speaker_embedding_interpolation.py"), run_name="__main__")
