#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
TTS_MODEL_DIR="${REPO_ROOT}/tts-model"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
SERVER_SCRIPT="${TTS_MODEL_DIR}/run_tts_server_impl.sh"
GRADIO_ENTRYPOINT="${TTS_MODEL_DIR}/gradio_demo.py"

resolve_repo_path() {
    local raw_path="$1"
    if [[ "${raw_path}" = /* ]]; then
        printf '%s\n' "${raw_path}"
    else
        raw_path="${raw_path#./}"
        printf '%s\n' "${REPO_ROOT}/${raw_path}"
    fi
}

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

TASK_TYPE="CustomVoice"
MODEL_SIZE="0.6B"
SERVER_HOST="${OPENCAST_TTS_HOST:-0.0.0.0}"
SERVER_PORT="${OPENCAST_TTS_PORT:-8091}"
GRADIO_HOST="${OPENCAST_GRADIO_HOST:-127.0.0.1}"
GRADIO_PORT="${OPENCAST_GRADIO_PORT:-7860}"
GPU_MEMORY_UTILIZATION="${OPENCAST_TTS_GPU_MEMORY_UTILIZATION:-0.3}"
MAX_MODEL_LEN="${OPENCAST_TTS_MAX_MODEL_LEN:-8192}"
STAGE_CONFIG_RAW="${OPENCAST_TTS_STAGE_CONFIG:-tts-model/qwen3_tts.yaml}"

usage() {
    local stage_label
    stage_label="$(resolve_repo_path "${STAGE_CONFIG_RAW}")"
    cat <<EOF
Usage: $0 [OPTIONS]

Options:
  --task-type TYPE        CustomVoice, VoiceDesign, or Base (default: ${TASK_TYPE})
  --model-size SIZE       0.6B or 1.7B (default: ${MODEL_SIZE})
  --server-host HOST      Bind host for vLLM-Omni (default: ${SERVER_HOST})
  --server-port PORT      Bind port for vLLM-Omni (default: ${SERVER_PORT})
  --gradio-host HOST      Bind host for Gradio (default: ${GRADIO_HOST})
  --gradio-port PORT      Bind port for Gradio (default: ${GRADIO_PORT})
  --gpu-memory-utilization VALUE
                          vLLM server memory fraction (default: ${GPU_MEMORY_UTILIZATION})
  --max-model-len TOKENS  Global max model length (default: ${MAX_MODEL_LEN})
  --stage-config PATH     Stage config path (default: ${stage_label})
  --help                  Show this help text
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --task-type)
            TASK_TYPE="$2"
            shift 2
            ;;
        --model-size)
            MODEL_SIZE="$2"
            shift 2
            ;;
        --server-host)
            SERVER_HOST="$2"
            shift 2
            ;;
        --server-port)
            SERVER_PORT="$2"
            shift 2
            ;;
        --gradio-host)
            GRADIO_HOST="$2"
            shift 2
            ;;
        --gradio-port)
            GRADIO_PORT="$2"
            shift 2
            ;;
        --gpu-memory-utilization)
            GPU_MEMORY_UTILIZATION="$2"
            shift 2
            ;;
        --max-model-len)
            MAX_MODEL_LEN="$2"
            shift 2
            ;;
        --stage-config)
            STAGE_CONFIG_RAW="$2"
            shift 2
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
done

STAGE_CONFIG="$(resolve_repo_path "${STAGE_CONFIG_RAW}")"

if [[ ! -f "${SERVER_SCRIPT}" ]]; then
    echo "Missing ${SERVER_SCRIPT}"
    exit 1
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing ${PYTHON_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

LOG_DIR="${REPO_ROOT}/.cache/opencast"
LOG_FILE="${LOG_DIR}/gradio-server-${SERVER_PORT}.log"
mkdir -p "${LOG_DIR}"
API_BASE="http://127.0.0.1:${SERVER_PORT}"

cleanup() {
    echo
    echo "Shutting down..."
    if [[ -n "${GRADIO_PID:-}" ]]; then
        kill "${GRADIO_PID}" 2>/dev/null || true
        wait "${GRADIO_PID}" 2>/dev/null || true
    fi
    if [[ -n "${SERVER_PID:-}" ]]; then
        kill "${SERVER_PID}" 2>/dev/null || true
        wait "${SERVER_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "Starting vLLM-Omni server..."
SERVER_CMD=(
    bash
    "${SERVER_SCRIPT}"
    --task-type "${TASK_TYPE}"
    --model-size "${MODEL_SIZE}"
    --host "${SERVER_HOST}"
    --port "${SERVER_PORT}"
    --max-model-len "${MAX_MODEL_LEN}"
    --stage-config "${STAGE_CONFIG}"
)
if [[ -n "${GPU_MEMORY_UTILIZATION}" ]]; then
    SERVER_CMD+=(--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}")
fi
"${SERVER_CMD[@]}" >"${LOG_FILE}" 2>&1 &
SERVER_PID=$!

echo "Waiting for ${API_BASE}/v1/audio/voices"
for _ in $(seq 1 300); do
    if curl -fsS "${API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
        break
    fi
    if ! kill -0 "${SERVER_PID}" 2>/dev/null; then
        echo "vLLM-Omni exited early. See ${LOG_FILE}"
        exit 1
    fi
    sleep 1
done

if ! curl -fsS "${API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
    echo "Timed out waiting for the TTS server. See ${LOG_FILE}"
    exit 1
fi

echo "Starting Gradio demo..."
"${PYTHON_BIN}" "${GRADIO_ENTRYPOINT}" \
    --api-base "${API_BASE}" \
    --host "${GRADIO_HOST}" \
    --port "${GRADIO_PORT}" &
GRADIO_PID=$!

echo
echo "vLLM Server : ${API_BASE}"
echo "Gradio Demo : http://${GRADIO_HOST}:${GRADIO_PORT}"
echo "Server log  : ${LOG_FILE}"
echo

wait "${GRADIO_PID}"
