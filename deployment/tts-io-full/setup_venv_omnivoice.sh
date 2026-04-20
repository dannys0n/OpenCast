#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${VENV_DIR:-$SCRIPT_DIR/.venv}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
PYTHON_BIN="python${PYTHON_VERSION}"
OMNIVOICE_SERVER_REPO="${OMNIVOICE_SERVER_REPO_PATH:-$SCRIPT_DIR/omnivoice-server}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu128}"
TORCH_VERSION="${TORCH_VERSION:-2.9.0+cu128}"
TORCHAUDIO_VERSION="${TORCHAUDIO_VERSION:-2.9.0+cu128}"
INSTALL_DEV_EXTRAS="${INSTALL_DEV_EXTRAS:-1}"

if [[ ! -d "$OMNIVOICE_SERVER_REPO" ]]; then
  echo "Missing omnivoice-server repo: $OMNIVOICE_SERVER_REPO" >&2
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  echo "Missing uv. Install uv first so Python ${PYTHON_VERSION} can be managed locally." >&2
  exit 1
fi

echo "Installing Python ${PYTHON_VERSION} via uv"
uv python install "$PYTHON_VERSION"

if [[ -d "$VENV_DIR" ]]; then
  echo "Removing existing virtualenv at $VENV_DIR"
  rm -rf "$VENV_DIR"
fi

echo "Creating virtualenv at $VENV_DIR with $PYTHON_BIN"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "Installing system packages required for OmniVoice playback and reference conversion"
sudo apt install -y sox ffmpeg libportaudio2

source "$VENV_DIR/bin/activate"

echo "Installing Python dependencies into $VENV_DIR"
python -m pip install --upgrade pip setuptools wheel
python -m pip install --index-url "$TORCH_INDEX_URL" "torch==$TORCH_VERSION" "torchaudio==$TORCHAUDIO_VERSION"

if [[ "$INSTALL_DEV_EXTRAS" == "1" ]]; then
  echo "Installing local omnivoice-server repo in editable mode with dev extras"
  (
    cd "$OMNIVOICE_SERVER_REPO"
    python -m pip install -e ".[dev]"
  )
else
  echo "Installing local omnivoice-server repo in editable mode"
  (
    cd "$OMNIVOICE_SERVER_REPO"
    python -m pip install -e .
  )
fi

echo "===================================================="
echo "OmniVoice environment is ready."
echo "Venv: $VENV_DIR"
echo "Repo: $OMNIVOICE_SERVER_REPO"
