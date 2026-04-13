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
SERVER_LOG="${SERVER_LOG:-/tmp/qwen3_tts_openai_fastapi_simultaneous_sequence.log}"
TMP_ROOT="${TMPDIR:-/tmp}"

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

if ! command -v mktemp >/dev/null 2>&1; then
  echo "mktemp is required on PATH" >&2
  exit 1
fi

SERVER_PID=""
PLAY_PID=""
TEMP_DIR=""
CURL_PIDS=()

cleanup() {
  local pid

  if [[ -n "$PLAY_PID" ]] && kill -0 "$PLAY_PID" >/dev/null 2>&1; then
    kill "$PLAY_PID" >/dev/null 2>&1 || true
    wait "$PLAY_PID" >/dev/null 2>&1 || true
  fi

  for pid in "${CURL_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
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

TEMP_DIR="$(mktemp -d "$TMP_ROOT/qwen3_tts_simultaneous_sequence.XXXXXX")"
AGGREGATE_FIFO="$TEMP_DIR/sequence.pcm"
mkfifo "$AGGREGATE_FIFO"

declare -a VOICES=(
  "clone:scrawny_e2"
  "clone:scrawny_e1"
  "clone:scrawny_e0"
)

declare -a TEXTS=(
  "This is scrawny e two."
  "This is scrawny e one."
  "This is scrawny e zero."
)

declare -a BUFFER_FILES=()
declare -a DONE_FILES=()
declare -a STATUS_FILES=()

build_request_json() {
  local voice_name="$1"
  local text="$2"

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
}

dispatch_request() {
  local index="$1"
  local voice_name="$2"
  local text="$3"
  local buffer_file="$TEMP_DIR/request_${index}.pcm"
  local done_file="$TEMP_DIR/request_${index}.done"
  local status_file="$TEMP_DIR/request_${index}.status"
  local request_json

  request_json="$(build_request_json "$voice_name" "$text")"
  : >"$buffer_file"
  BUFFER_FILES+=("$buffer_file")
  DONE_FILES+=("$done_file")
  STATUS_FILES+=("$status_file")

  echo "Dispatching ${voice_name} for queued playback"
  (
    status=0
    if curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "http://$HOST:$PORT/v1/audio/speech" \
      >"$buffer_file"; then
      status=0
    else
      status=$?
    fi
    printf '%s\n' "$status" >"$status_file"
    touch "$done_file"
    exit "$status"
  ) &
  CURL_PIDS+=("$!")
}

stream_buffer_into_fd() {
  local buffer_file="$1"
  local done_file="$2"
  local status_file="$3"
  local offset=0
  local size=0
  local to_copy=0
  local status=0

  while :; do
    size="$(stat -c '%s' "$buffer_file" 2>/dev/null || printf '0')"
    if (( size > offset )); then
      to_copy=$((size - offset))
      dd if="$buffer_file" bs=1 skip="$offset" count="$to_copy" status=none >&3
      offset="$size"
      continue
    fi

    if [[ -f "$done_file" ]]; then
      if [[ -f "$status_file" ]]; then
        status="$(<"$status_file")"
      fi
      if [[ "$status" != "0" ]]; then
        echo "Request failed while streaming $buffer_file" >&2
        return 1
      fi
      break
    fi

    sleep 0.01
  done
}

echo "Starting seamless playback pipeline..."
play -q -t raw -b 16 -e signed-integer -c 1 -r 24000 "$AGGREGATE_FIFO" &
PLAY_PID="$!"

exec 3>"$AGGREGATE_FIFO"

for i in "${!VOICES[@]}"; do
  dispatch_request "$i" "${VOICES[$i]}" "${TEXTS[$i]}"
done

echo
echo "All requests dispatched. Streaming them in order through one playback session..."

for i in "${!BUFFER_FILES[@]}"; do
  echo "Queueing ${VOICES[$i]}"
  stream_buffer_into_fd "${BUFFER_FILES[$i]}" "${DONE_FILES[$i]}" "${STATUS_FILES[$i]}"
done

exec 3>&-

for pid in "${CURL_PIDS[@]}"; do
  wait "$pid"
done

wait "$PLAY_PID"

echo
echo "Simultaneous queued playback finished."
