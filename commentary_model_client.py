#!/usr/bin/env python3

"""Compatibility wrapper for the commentary model client."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

from opencast_bootstrap import maybe_reexec_with_repo_venv

REPO_ROOT = Path(__file__).resolve().parent
TEXT_MODEL_DIR = REPO_ROOT / "text-model"

maybe_reexec_with_repo_venv(__file__, REPO_ROOT)
sys.path.insert(0, str(TEXT_MODEL_DIR))

runpy.run_path(str(TEXT_MODEL_DIR / "commentary_model_client.py"), run_name="__main__")
