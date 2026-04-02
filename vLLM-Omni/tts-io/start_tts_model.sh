#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
MODEL_NAME="Qwen/Qwen3-TTS-12Hz-0.6B-Base"
SERVER_PORT="8091"
CUDA_DEVICE="${CUDA_VISIBLE_DEVICES:-0}"
STAGE_CONFIG_PATH="$REPO_ROOT/.venv/lib/python3.12/site-packages/vllm_omni/model_executor/stage_configs/qwen3_tts.yaml"
HF_HOME_DIR="$REPO_ROOT/.hf-cache"
HF_HUB_DIR="$HF_HOME_DIR/hub"

cd "$REPO_ROOT"

if [ ! -d "$REPO_ROOT/.venv" ]; then
  echo "Missing .venv. Run sh tts-io/setup_linux_env.sh first."
  exit 1
fi

source "$REPO_ROOT/.venv/bin/activate"

mkdir -p "$HF_HOME_DIR" "$HF_HUB_DIR"

export HF_HOME="$HF_HOME_DIR"
export HF_HUB_CACHE="$HF_HUB_DIR"
export CUDA_VISIBLE_DEVICES="$CUDA_DEVICE"
export PYTORCH_ALLOC_CONF="${PYTORCH_ALLOC_CONF:-expandable_segments:True}"

echo "Starting $MODEL_NAME on port $SERVER_PORT"

exec bash -lc "
  source \"$REPO_ROOT/.venv/bin/activate\"
  export HF_HOME=\"$HF_HOME_DIR\"
  export HF_HUB_CACHE=\"$HF_HUB_DIR\"
  export CUDA_VISIBLE_DEVICES=\"$CUDA_DEVICE\"
  export PYTORCH_ALLOC_CONF=\"${PYTORCH_ALLOC_CONF:-expandable_segments:True}\"
  vllm-omni serve \"$MODEL_NAME\" \
    --omni \
    --stage-configs-path \"$STAGE_CONFIG_PATH\" \
    --port \"$SERVER_PORT\" \
    --trust-remote-code \
    --enforce-eager
"
