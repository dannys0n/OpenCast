#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
TTS_MODEL_DIR="${REPO_ROOT}/tts-model"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
VLLM_OMNI_BIN="${REPO_ROOT}/.venv/bin/vllm-omni"

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
HOST="${OPENCAST_TTS_HOST:-0.0.0.0}"
PORT="${OPENCAST_TTS_PORT:-8091}"
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
  --host HOST             Bind host (default: ${HOST})
  --port PORT             Bind port (default: ${PORT})
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
        --host)
            HOST="$2"
            shift 2
            ;;
        --port)
            PORT="$2"
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

if [[ ! -x "${VLLM_OMNI_BIN}" ]]; then
    echo "Missing ${VLLM_OMNI_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

if [[ ! -f "${STAGE_CONFIG}" ]]; then
    echo "Stage config not found: ${STAGE_CONFIG}"
    exit 1
fi

SITE_PACKAGES_DIR="$("${PYTHON_BIN}" -c "import site; print(site.getsitepackages()[0])")"
LIB_DIRS=("${SITE_PACKAGES_DIR}/torch/lib")
for candidate in "${SITE_PACKAGES_DIR}"/nvidia/*/lib; do
    if [[ -d "${candidate}" ]]; then
        LIB_DIRS+=("${candidate}")
    fi
done
LD_PREFIX="$(IFS=:; echo "${LIB_DIRS[*]}")"
export LD_LIBRARY_PATH="${LD_PREFIX}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export PYTHONPATH="${TTS_MODEL_DIR}:${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

export HF_HOME="${REPO_ROOT}/.cache/huggingface"
export HUGGINGFACE_HUB_CACHE="${HF_HOME}/hub"
mkdir -p "${HUGGINGFACE_HUB_CACHE}"

case "${TASK_TYPE}" in
    CustomVoice)
        case "${MODEL_SIZE}" in
            0.6B) MODEL="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" ;;
            1.7B) MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-CustomVoice" ;;
            *) echo "Unsupported model size for ${TASK_TYPE}: ${MODEL_SIZE}"; exit 1 ;;
        esac
        ;;
    Base)
        case "${MODEL_SIZE}" in
            0.6B) MODEL="Qwen/Qwen3-TTS-12Hz-0.6B-Base" ;;
            1.7B) MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-Base" ;;
            *) echo "Unsupported model size for ${TASK_TYPE}: ${MODEL_SIZE}"; exit 1 ;;
        esac
        ;;
    VoiceDesign)
        if [[ "${MODEL_SIZE}" != "1.7B" ]]; then
            echo "VoiceDesign is only documented for the 1.7B model."
            exit 1
        fi
        MODEL="Qwen/Qwen3-TTS-12Hz-1.7B-VoiceDesign"
        ;;
    *)
        echo "Unsupported task type: ${TASK_TYPE}"
        exit 1
        ;;
esac

echo "Starting Qwen3-TTS server"
echo "  model: ${MODEL}"
echo "  host : ${HOST}"
echo "  port : ${PORT}"
echo "  stage: ${STAGE_CONFIG}"

CMD=(
    "${VLLM_OMNI_BIN}"
    serve
    "${MODEL}"
    --stage-configs-path "${STAGE_CONFIG}"
    --host "${HOST}"
    --port "${PORT}"
    --max-model-len "${MAX_MODEL_LEN}"
    --trust-remote-code
    --omni
)

if [[ -n "${GPU_MEMORY_UTILIZATION}" ]]; then
    CMD+=(--gpu-memory-utilization "${GPU_MEMORY_UTILIZATION}")
fi

exec "${CMD[@]}"
