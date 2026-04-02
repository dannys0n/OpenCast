#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
COMMENTARY_CLIENT="${REPO_ROOT}/text-model/commentary_model_client.py"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

DMR_MODEL="${OPENCAST_COMMENTARY_MODEL:-huggingface.co/qwen/qwen2.5-0.5b-instruct-gguf:Q4_K_M}"
DMR_CONTEXT_SIZE="${OPENCAST_COMMENTARY_DMR_CONTEXT_SIZE:-2048}"
DMR_GPU_LAYERS="${OPENCAST_COMMENTARY_DMR_GPU_LAYERS:-8}"
DMR_BASE_URL="${OPENCAST_COMMENTARY_DMR_BASE_URL:-http://localhost:12434/engines/v1}"
DMR_WARMUP_TEXT="${OPENCAST_COMMENTARY_DMR_WARMUP_TEXT:-Blue side have Baron control and red team are late to the setup.}"

if ! command -v docker >/dev/null 2>&1; then
    echo "Missing docker. Install Docker Engine or Docker Desktop with Model Runner support first."
    exit 1
fi

if ! docker model version >/dev/null 2>&1; then
    echo "docker model is not available. Install or enable Docker Model Runner first."
    exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing ${PYTHON_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

echo "Reinstalling Docker Model Runner in GPU llama.cpp mode..."
docker model reinstall-runner --backend llama.cpp --gpu cuda

echo "Waiting for Docker Model Runner to come back..."
for _ in $(seq 1 60); do
    if curl -fsS "${DMR_BASE_URL}/models" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done

if ! curl -fsS "${DMR_BASE_URL}/models" >/dev/null 2>&1; then
    echo "Docker Model Runner did not become ready at ${DMR_BASE_URL}."
    exit 1
fi

echo "Configuring ${DMR_MODEL} for shared-GPU coexistence..."
docker model configure "${DMR_MODEL}" --context-size "${DMR_CONTEXT_SIZE}" -- --n-gpu-layers "${DMR_GPU_LAYERS}"

echo "Priming the commentary model so it claims VRAM before the TTS server starts..."
if ! "${PYTHON_BIN}" "${COMMENTARY_CLIENT}" \
    --base-url "${DMR_BASE_URL}" \
    --model "${DMR_MODEL}" \
    --check-model \
    --match-state "${DMR_WARMUP_TEXT}" >/dev/null; then
    echo
    echo "Failed to warm the GPU-backed commentary model."
    echo "If the TTS server is already running, stop it and run this script first."
    exit 1
fi

echo
echo "Docker Model Runner is ready for the commentary bridge."
echo "Model       : ${DMR_MODEL}"
echo "Context size: ${DMR_CONTEXT_SIZE}"
echo "GPU layers  : ${DMR_GPU_LAYERS}"
echo "Warmup      : complete"
