"""Stdlib-only bootstrap helpers for executable repo scripts."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def maybe_reexec_with_repo_venv(current_file: str | Path, repo_root: str | Path | None = None) -> None:
    """Re-exec the current script with the repo's virtualenv Python if available."""
    current_path = Path(current_file).resolve()
    root = Path(repo_root).resolve() if repo_root is not None else current_path.parent
    venv_python = root / ".venv" / "bin" / "python"

    if not venv_python.exists():
        return
    if os.environ.get("OPENCAST_SKIP_VENV_REEXEC") == "1":
        return

    try:
        current_executable = Path(sys.executable).resolve()
    except Exception:
        current_executable = None

    if current_executable == venv_python.resolve():
        return

    env = os.environ.copy()
    env["OPENCAST_SKIP_VENV_REEXEC"] = "1"
    os.execve(
        str(venv_python),
        [str(venv_python), str(current_path), *sys.argv[1:]],
        env,
    )
