#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVER_DIR="$ROOT_DIR/omnivoice-server"
ENV_FILE="$ROOT_DIR/.env"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST="${OMNIVOICE_TEST_HOST:-127.0.0.1}"
PORT="${OMNIVOICE_TEST_PORT:-8881}"
API_BASE="${OMNIVOICE_TEST_API_BASE:-http://$HOST:$PORT}"
MODEL_ID="${OMNIVOICE_TEST_MODEL_ID:-${OMNIVOICE_MODEL_ID:-k2-fsa/OmniVoice}}"
DEVICE="${OMNIVOICE_TEST_DEVICE:-${OMNIVOICE_DEVICE:-cpu}}"
NUM_STEP="${OMNIVOICE_TEST_NUM_STEP:-8}"
REQUEST_TIMEOUT_S="${OMNIVOICE_TEST_REQUEST_TIMEOUT_S:-120}"
TEXT="${*:-This is a short streamed OmniVoice prompt test. It should return PCM chunks as they are ready.}"
VOICE_NAME="${OMNIVOICE_TEST_VOICE:-ballad}"
INSTRUCTIONS="${OMNIVOICE_TEST_INSTRUCTIONS:-}"
MODEL_NAME="${OMNIVOICE_TEST_MODEL_NAME:-tts-1}"
SERVER_LOG="${OMNIVOICE_TEST_SERVER_LOG:-/tmp/omnivoice_server_test.log}"
TMP_ROOT="${TMPDIR:-/tmp}"
STATE_DIR="${OMNIVOICE_TEST_STATE_DIR:-$ROOT_DIR/.state/omnivoice-server-test}"
PROFILE_DIR="${OMNIVOICE_TEST_PROFILE_DIR:-$STATE_DIR/profiles}"
MODEL_CACHE_DIR="${OMNIVOICE_TEST_MODEL_CACHE_DIR:-$STATE_DIR/hf-cache}"
XDG_CACHE_HOME_DIR="${OMNIVOICE_TEST_XDG_CACHE_HOME:-$STATE_DIR/xdg-cache}"
TEMP_DIR=""
KEEP_OUTPUT="${OMNIVOICE_TEST_KEEP_OUTPUT:-1}"
STARTED_SERVER="0"
SERVER_PID=""
AUTO_CREATE_CAST_CLONES="${OMNIVOICE_AUTO_CREATE_CAST_CLONES:-1}"
CAST_CLONE_HELPER="${OMNIVOICE_CAST_CLONE_HELPER:-$ROOT_DIR/omnivoice_test/create_cast_clone_profiles.sh}"

if [[ ! -d "$SERVER_DIR" ]]; then
  echo "Missing omnivoice-server checkout at $SERVER_DIR" >&2
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on PATH" >&2
  exit 1
fi

find_listener_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "TCP:${port}" -sTCP:LISTEN 2>/dev/null || true
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser "${port}/tcp" 2>/dev/null | tr ' ' '\n' || true
  fi
}

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
  if [[ "$KEEP_OUTPUT" == "0" && -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

trap cleanup EXIT INT TERM

wait_for_health() {
  local attempts="${1:-180}"
  local last_status=""
  for _ in $(seq 1 "$attempts"); do
    if last_status="$(curl -fsS "$API_BASE/health" 2>/dev/null)"; then
      printf '%s\n' "$last_status"
      return 0
    fi
    if [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      echo "omnivoice-server exited before becoming healthy. Tail of log:" >&2
      tail -n 80 "$SERVER_LOG" >&2 || true
      return 1
    fi
    sleep 1
  done

  echo "Timed out waiting for $API_BASE/health" >&2
  tail -n 80 "$SERVER_LOG" >&2 || true
  return 1
}

if ! curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
  existing_pids="$(find_listener_pids "$PORT" | sed '/^$/d' | tr '\n' ' ')"
  if [[ -n "$existing_pids" ]]; then
    echo "Port $PORT is already in use by: $existing_pids" >&2
    exit 1
  fi

  echo "Starting omnivoice-server on $API_BASE"
  echo "  model:   $MODEL_ID"
  echo "  device:  $DEVICE"
  echo "  log:     $SERVER_LOG"
  echo "  profile: $PROFILE_DIR"
  echo "  cache:   $MODEL_CACHE_DIR"

  mkdir -p "$PROFILE_DIR" "$MODEL_CACHE_DIR" "$XDG_CACHE_HOME_DIR"
  : >"$SERVER_LOG"
  (
    cd "$SERVER_DIR"
    env -i \
      PATH="$PATH" \
      HOME="$HOME" \
      PYTHONPATH="$SERVER_DIR" \
      XDG_CACHE_HOME="$XDG_CACHE_HOME_DIR" \
      HF_HOME="$MODEL_CACHE_DIR" \
      HF_HUB_CACHE="$MODEL_CACHE_DIR/hub" \
      HUGGINGFACE_HUB_CACHE="$MODEL_CACHE_DIR/hub" \
      HF_HUB_DISABLE_XET=1 \
      OMNIVOICE_HOST="$HOST" \
      OMNIVOICE_PORT="$PORT" \
      OMNIVOICE_MODEL_ID="$MODEL_ID" \
      OMNIVOICE_MODEL_CACHE_DIR="$MODEL_CACHE_DIR" \
      OMNIVOICE_DEVICE="$DEVICE" \
      OMNIVOICE_NUM_STEP="$NUM_STEP" \
      OMNIVOICE_PROFILE_DIR="$PROFILE_DIR" \
      OMNIVOICE_REQUEST_TIMEOUT_S="$REQUEST_TIMEOUT_S" \
      "$VENV_PYTHON" -m omnivoice_server.cli >>"$SERVER_LOG" 2>&1
  ) &
  SERVER_PID="$!"
  STARTED_SERVER="1"
fi

HEALTH_JSON="$(wait_for_health)"

if [[ "$AUTO_CREATE_CAST_CLONES" == "1" && -x "$CAST_CLONE_HELPER" ]]; then
  OMNIVOICE_API_BASE="$API_BASE" \
  OMNIVOICE_PROFILE_DIR="$PROFILE_DIR" \
  "$CAST_CLONE_HELPER" >/dev/null
fi

TEMP_DIR="$(mktemp -d "$TMP_ROOT/omnivoice_test_prompt.XXXXXX")"
PCM_PATH="$TEMP_DIR/output.pcm"
WAV_PATH="$TEMP_DIR/output.wav"

echo
echo "Health:"
printf '%s\n' "$HEALTH_JSON"
echo
echo "Prompt:"
echo "  text:         $TEXT"
echo "  voice:        $VOICE_NAME"
if [[ -n "$INSTRUCTIONS" ]]; then
  echo "  instructions: $INSTRUCTIONS"
fi
echo "  output pcm:   $PCM_PATH"

TEXT="$TEXT" \
VOICE_NAME="$VOICE_NAME" \
INSTRUCTIONS="$INSTRUCTIONS" \
MODEL_NAME="$MODEL_NAME" \
API_BASE="$API_BASE" \
PCM_PATH="$PCM_PATH" \
"$VENV_PYTHON" - <<'PY'
import json
import os
import time
from pathlib import Path

import httpx

api_base = os.environ["API_BASE"].rstrip("/")
pcm_path = Path(os.environ["PCM_PATH"])
payload = {
    "model": os.environ["MODEL_NAME"],
    "input": os.environ["TEXT"],
    "voice": os.environ["VOICE_NAME"],
    "stream": True,
    "response_format": "pcm",
}
if os.environ.get("INSTRUCTIONS"):
    payload["instructions"] = os.environ["INSTRUCTIONS"]

start = time.monotonic()
first_chunk_at = None
chunk_count = 0
total_bytes = 0
headers = None

with httpx.stream(
    "POST",
    f"{api_base}/v1/audio/speech",
    headers={"Content-Type": "application/json"},
    json=payload,
    timeout=180.0,
) as response:
    response.raise_for_status()
    headers = dict(response.headers)
    with pcm_path.open("wb") as handle:
      for chunk in response.iter_bytes():
          if not chunk:
              continue
          if first_chunk_at is None:
              first_chunk_at = time.monotonic()
          chunk_count += 1
          total_bytes += len(chunk)
          handle.write(chunk)

elapsed = time.monotonic() - start
ttfb = None if first_chunk_at is None else first_chunk_at - start
prefix = pcm_path.read_bytes()[:4] if pcm_path.exists() else b""

print(json.dumps({
    "status": "ok",
    "chunk_count": chunk_count,
    "total_bytes": total_bytes,
    "elapsed_s": round(elapsed, 3),
    "time_to_first_chunk_s": None if ttfb is None else round(ttfb, 3),
    "content_type": headers.get("content-type") if headers else None,
    "x_audio_sample_rate": headers.get("x-audio-sample-rate") if headers else None,
    "x_audio_format": headers.get("x-audio-format") if headers else None,
    "riff_prefix": prefix.decode("latin1", errors="replace"),
}, indent=2))
PY

if command -v ffmpeg >/dev/null 2>&1; then
  ffmpeg -y -f s16le -ar 24000 -ac 1 -i "$PCM_PATH" "$WAV_PATH" >/dev/null 2>&1
  echo
  echo "WAV copy written to $WAV_PATH"
fi

if [[ "$STARTED_SERVER" == "1" ]]; then
  echo
  echo "Stopping omnivoice-server test instance."
fi
