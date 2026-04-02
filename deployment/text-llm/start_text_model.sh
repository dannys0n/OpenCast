#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
MODEL_NAME_DEFAULT="hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

MODEL_NAME="${MODEL_NAME:-$MODEL_NAME_DEFAULT}"

docker model run "$MODEL_NAME"
