#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
OPENAI_CLIENT="${REPO_ROOT}/tts-model/openai_speech_client.py"
WS_CLIENT="${REPO_ROOT}/tts-model/streaming_speech_client.py"
OUTPUT_DIR="${REPO_ROOT}/.cache/opencast/smoke-tests"
MODE="${1:-all}"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

API_BASE="${OPENCAST_TTS_API_BASE:-http://localhost:8091}"
WS_URL="${OPENCAST_TTS_WS_URL:-ws://localhost:8091/v1/audio/speech/stream}"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing ${PYTHON_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

mkdir -p "${OUTPUT_DIR}"

run_http_stream() {
    echo "==> HTTP streaming PCM smoke test"
    "${PYTHON_BIN}" "${OPENAI_CLIENT}" \
        --api-base "${API_BASE}" \
        --model "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" \
        --task-type CustomVoice \
        --speaker vivian \
        --language English \
        --text "The dragon is down. Blue team can push for the finish." \
        --stream \
        --response-format pcm \
        --output "${OUTPUT_DIR}/http_stream.pcm"
}

run_voice_clone() {
    echo "==> Base voice cloning smoke test"
    "${PYTHON_BIN}" "${OPENAI_CLIENT}" \
        --api-base "${API_BASE}" \
        --model "Qwen/Qwen3-TTS-12Hz-0.6B-Base" \
        --task-type Base \
        --language English \
        --text "This is a quick smoke test for cloned voice output." \
        --ref-audio "https://qianwen-res.oss-cn-beijing.aliyuncs.com/Qwen3-TTS-Repo/clone_2.wav" \
        --x-vector-only \
        --output "${OUTPUT_DIR}/voice_clone.wav"
}

run_ws_stream() {
    echo "==> WebSocket incremental text-input smoke test"
    "${PYTHON_BIN}" "${WS_CLIENT}" \
        --url "${WS_URL}" \
        --model "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice" \
        --task-type CustomVoice \
        --speaker vivian \
        --language English \
        --response-format pcm \
        --stream-audio \
        --simulate-stt \
        --stt-delay 0.05 \
        --text "Blue team are rotating to Baron. Red team are late to the pit. This could be the deciding play." \
        --output-dir "${OUTPUT_DIR}/ws_stream"
}

case "${MODE}" in
    http-stream)
        run_http_stream
        ;;
    voice-clone)
        run_voice_clone
        ;;
    ws-stream)
        run_ws_stream
        ;;
    all)
        run_http_stream
        run_voice_clone
        run_ws_stream
        ;;
    *)
        echo "Usage: $0 [http-stream|voice-clone|ws-stream|all]"
        exit 1
        ;;
esac

echo
echo "Smoke test outputs saved under ${OUTPUT_DIR}"
