#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="${OPENCAST_REPO_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}"
TEXT_MODEL_DIR="${REPO_ROOT}/text-model"
TTS_MODEL_DIR="${REPO_ROOT}/tts-model"
ENV_FILE="${REPO_ROOT}/.env"
PYTHON_BIN="${REPO_ROOT}/.venv/bin/python"
BRIDGE_SCRIPT="${TEXT_MODEL_DIR}/live_commentary_bridge.py"

if [[ -f "${ENV_FILE}" ]]; then
    # shellcheck disable=SC1090
    source "${ENV_FILE}"
fi

export PYTHONPATH="${TEXT_MODEL_DIR}:${TTS_MODEL_DIR}:${REPO_ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

DMR_BASE_URL="${OPENCAST_COMMENTARY_DMR_BASE_URL:-http://localhost:12434/engines/v1}"
DMR_MODEL="${OPENCAST_COMMENTARY_MODEL:-huggingface.co/qwen/qwen2.5-0.5b-instruct-gguf:Q4_K_M}"
TTS_API_BASE="${OPENCAST_TTS_API_BASE:-http://localhost:8091}"

usage() {
    cat <<EOF
Usage: $0 [bridge options]

This wrapper verifies Docker Model Runner and the local TTS API before it
starts the Python bridge. Any extra arguments are passed to
text-model/live_commentary_bridge.py.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    echo
    "${PYTHON_BIN}" "${BRIDGE_SCRIPT}" --help
    exit 0
fi

if [[ ! -x "${PYTHON_BIN}" ]]; then
    echo "Missing ${PYTHON_BIN}. Run ./setup_host_env.sh first."
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "Missing docker. Install Docker Desktop or Docker Engine with Model Runner support first."
    exit 1
fi

if ! docker model version >/dev/null 2>&1; then
    echo "docker model is not available. Install or enable Docker Model Runner first."
    exit 1
fi

MODEL_JSON_FILE="$(mktemp)"
trap 'rm -f "${MODEL_JSON_FILE}"' EXIT

if ! curl -fsS "${DMR_BASE_URL}/models" >"${MODEL_JSON_FILE}" 2>/dev/null; then
    echo "Docker Model Runner API is not reachable at ${DMR_BASE_URL}."
    echo "Warm it up with:"
    echo "  docker model run hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
    exit 1
fi

if ! "${PYTHON_BIN}" - "${DMR_MODEL}" "${MODEL_JSON_FILE}" <<'PY'
import json
import sys
from pathlib import Path

model_id = sys.argv[1]
payload = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
models = {entry.get("id") for entry in payload.get("data") or []}
if model_id not in models:
    available = ", ".join(sorted(model for model in models if model))
    raise SystemExit(
        f"Configured commentary model {model_id!r} is not available. "
        f"Available models: {available or 'none'}"
    )
PY
then
    exit 1
fi

if ! curl -fsS "${TTS_API_BASE}/v1/audio/voices" >/dev/null 2>&1; then
    echo "TTS API is not reachable at ${TTS_API_BASE}."
    echo "Start it first with something like:"
    echo "  bash ./run_tts_server.sh --task-type CustomVoice --model-size 0.6B"
    exit 1
fi

exec "${PYTHON_BIN}" "${BRIDGE_SCRIPT}" "$@"
