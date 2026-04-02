#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON_VERSION="3.12"
TORCH_BACKEND="cu129"
VENV_DIR="$REPO_ROOT/.venv"
HF_CACHE_DIR="$REPO_ROOT/.hf-cache"

ensure_command() {
  local cmd="$1"
  local hint="$2"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd"
    echo "Install it with: $hint"
    exit 1
  fi
}

if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

export PATH="$HOME/.local/bin:$PATH"

ensure_command uv "curl -LsSf https://astral.sh/uv/install.sh | sh"
ensure_command ffmpeg "sudo apt update && sudo apt install -y ffmpeg"
ensure_command play "sudo apt update && sudo apt install -y sox"
ensure_command nvidia-smi "install the NVIDIA driver and confirm the GPU is available"

echo "Using repo root: $REPO_ROOT"
echo "Checking GPU..."
nvidia-smi >/dev/null

echo "Installing Python $PYTHON_VERSION with uv if needed..."
uv python install "$PYTHON_VERSION"

if [ ! -d "$VENV_DIR" ]; then
  echo "Creating virtual environment..."
  uv venv "$VENV_DIR" --python "$PYTHON_VERSION" --seed
fi

source "$VENV_DIR/bin/activate"

echo "Installing vllm and vllm-omni..."
uv pip install --reinstall --refresh --torch-backend="$TORCH_BACKEND" vllm vllm-omni

mkdir -p "$HF_CACHE_DIR" "$HF_CACHE_DIR/hub"

echo
echo "Setup complete."
echo "Activate with:"
echo "  cd \"$REPO_ROOT\""
echo "  source .venv/bin/activate"
echo
echo "Start the Base TTS model with:"
echo "  sh tts-io/start_tts_model.sh"
