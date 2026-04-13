#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/Qwen3-TTS-Openai-Fastapi"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
CONFIG_FILE="${TTS_CONFIG:-$PROJECT_DIR/config.opencast.local.yaml}"
VOICE_LIBRARY_DIR="${VOICE_LIBRARY_DIR:-$PROJECT_DIR/voice_library}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8880}"
SERVER_LOG="${SERVER_LOG:-/tmp/qwen3_tts_openai_fastapi_sequence_send.log}"

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Missing project dir: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing config file: $CONFIG_FILE" >&2
  exit 1
fi

if [[ ! -d "$VOICE_LIBRARY_DIR" ]]; then
  echo "Missing voice library dir: $VOICE_LIBRARY_DIR" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on PATH" >&2
  exit 1
fi

if ! command -v play >/dev/null 2>&1; then
  echo "SoX 'play' is required on PATH" >&2
  exit 1
fi

SERVER_PID=""
REQUEST_PIDS=()
REQUEST_LABELS=()

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

echo "Starting optimized FastAPI TTS server..."
(
  cd "$PROJECT_DIR"
  export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"
  export TTS_BACKEND="optimized"
  export TTS_CONFIG="$CONFIG_FILE"
  export VOICE_LIBRARY_DIR="$VOICE_LIBRARY_DIR"
  export HOST="$HOST"
  export PORT="$PORT"
  exec "$VENV_PYTHON" -m api.main
) >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

echo "Waiting for server on http://$HOST:$PORT ..."
for _ in $(seq 1 120); do
  if curl -fsS "http://$HOST:$PORT/v1/voices" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://$HOST:$PORT/v1/voices" >/dev/null 2>&1; then
  echo "Server failed to become ready. Recent log:" >&2
  tail -n 120 "$SERVER_LOG" >&2 || true
  exit 1
fi

play_voice_async() {
  local voice_name="$1"
  local text="$2"
  local request_json

  request_json="$(
    VOICE_NAME="$voice_name" TEXT="$text" "$VENV_PYTHON" - <<'PY'
import json
import os

print(json.dumps({
    "model": "tts-1",
    "voice": os.environ["VOICE_NAME"],
    "input": os.environ["TEXT"],
    "stream": True,
    "response_format": "pcm",
}))
PY
  )"

  echo "Dispatching ${voice_name} for immediate playback"
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$request_json" \
    "http://$HOST:$PORT/v1/audio/speech" \
    | play -q -t raw -b 16 -e signed-integer -c 1 -r 24000 - &

  REQUEST_PIDS+=("$!")
  REQUEST_LABELS+=("${voice_name}")
}

play_voice_async "clone:announcer_e0" "This is the announcer voice."
play_voice_async "clone:turret_e0" "This is the turret voice."

echo
echo "All playback streams dispatched. Waiting for them to finish..."

for i in "${!REQUEST_PIDS[@]}"; do
  pid="${REQUEST_PIDS[$i]}"
  label="${REQUEST_LABELS[$i]}"
  if wait "$pid"; then
    echo "Completed ${label}"
  else
    echo "Request failed: ${label}" >&2
    exit 1
  fi
done

echo
echo "Sequence send finished."
