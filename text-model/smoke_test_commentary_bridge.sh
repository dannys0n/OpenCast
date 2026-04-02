#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
BRIDGE_SCRIPT="${REPO_ROOT}/run_commentary_bridge.sh"
SETUP_SCRIPT="${REPO_ROOT}/setup_commentary_model_runner.sh"
TTS_SERVER_SCRIPT="${REPO_ROOT}/run_tts_server.sh"
COMMENTARY_CLIENT="${REPO_ROOT}/text-model/commentary_model_client.py"
SAMPLE_INPUT="${REPO_ROOT}/text-model/examples/sample_match_state.txt"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

DMR_BASE_URL="${OPENCAST_COMMENTARY_DMR_BASE_URL:-http://localhost:12434/engines/v1}"
DMR_MODEL="${OPENCAST_COMMENTARY_MODEL:-huggingface.co/qwen/qwen2.5-0.5b-instruct-gguf:Q4_K_M}"
TTS_API_BASE="${OPENCAST_TTS_API_BASE:-http://localhost:8091}"
OUTPUT_ROOT="${REPO_ROOT}/.cache/opencast/commentary-smoke"
RUN_DIR="${OUTPUT_ROOT}/$(date +%Y%m%d-%H%M%S)"
PREVIEW_FILE="${RUN_DIR}/preview_commentary.txt"
TRANSCRIPT_FILE="${RUN_DIR}/commentary_transcript.txt"
AUDIO_DIR="${RUN_DIR}/audio"

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing ${PYTHON_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

mkdir -p "${AUDIO_DIR}"

cleanup() {
    if [[ -n "${STARTED_TTS_PID:-}" ]]; then
        kill "${STARTED_TTS_PID}" 2>/dev/null || true
        wait "${STARTED_TTS_PID}" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "==> Preparing Docker Model Runner for GPU-first commentary inference"
bash "${SETUP_SCRIPT}"

if ! curl -fsS "${TTS_API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
    echo "==> Starting TTS server after commentary warmup"
    bash "${TTS_SERVER_SCRIPT}" --task-type CustomVoice --model-size 0.6B >"${RUN_DIR}/tts-server.log" 2>&1 &
    STARTED_TTS_PID=$!

    for _ in $(seq 1 300); do
        if curl -fsS "${TTS_API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
            break
        fi
        if ! kill -0 "${STARTED_TTS_PID}" 2>/dev/null; then
            echo "TTS server exited early. See ${RUN_DIR}/tts-server.log"
            exit 1
        fi
        sleep 1
    done

    if ! curl -fsS "${TTS_API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
        echo "Timed out waiting for the TTS server. See ${RUN_DIR}/tts-server.log"
        exit 1
    fi
fi

echo "==> Checking Docker Model Runner models endpoint"
curl -fsS "${DMR_BASE_URL}/models" >/dev/null

echo "==> Checking configured commentary model"
"${PYTHON_BIN}" "${COMMENTARY_CLIENT}" --base-url "${DMR_BASE_URL}" --model "${DMR_MODEL}" --check-model --match-state "Blue side have Baron control and red team are late to the setup." >"${PREVIEW_FILE}"

echo "==> Preview commentary"
cat "${PREVIEW_FILE}"

echo "==> Running commentary bridge"
bash "${BRIDGE_SCRIPT}" \
    --input-file "${SAMPLE_INPUT}" \
    --output-dir "${AUDIO_DIR}" \
    --transcript-file "${TRANSCRIPT_FILE}" \
    --no-play-live

echo "==> Verifying transcript and sentence files"
"${PYTHON_BIN}" - "${TRANSCRIPT_FILE}" "${AUDIO_DIR}" <<'PY'
import sys
from pathlib import Path

transcript = Path(sys.argv[1])
audio_dir = Path(sys.argv[2])

text = transcript.read_text(encoding="utf-8")
commentary_lines = [line for line in text.splitlines() if line.startswith("[commentary ")]
audio_files = sorted(audio_dir.glob("sentence_*.pcm"))

if len(commentary_lines) < 3:
    raise SystemExit(f"Expected at least 3 commentary lines, found {len(commentary_lines)}")
if len(audio_files) < 3:
    raise SystemExit(f"Expected at least 3 sentence files, found {len(audio_files)}")

print(f"Verified {len(commentary_lines)} commentary lines and {len(audio_files)} sentence files.")
PY

echo
echo "Commentary smoke outputs saved under ${RUN_DIR}"
