#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_DIR="$SCRIPT_DIR/omnivoice-server"
ENV_FILE="$SCRIPT_DIR/.env"
OMNIVOICE_ENV_FILE="${OMNIVOICE_SERVER_ENV_FILE:-$SERVER_DIR/.env}"
OPENCAST_OMNIVOICE_ENV_FILE="${OPENCAST_OMNIVOICE_ENV_FILE:-$SERVER_DIR/.opencast.env}"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -f "$OMNIVOICE_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OMNIVOICE_ENV_FILE"
  set +a
fi

HOST="${OMNIVOICE_HOST:-${TTS_SERVER_HOST:-127.0.0.1}}"
PORT="${OMNIVOICE_PORT:-${TTS_SERVER_PORT:-8881}}"
API_BASE="${OMNIVOICE_API_BASE:-${TTS_API_BASE:-http://$HOST:$PORT}}"
MODEL_ID="${OMNIVOICE_MODEL_ID:-k2-fsa/OmniVoice}"
DEVICE="${OMNIVOICE_DEVICE:-}"
NUM_STEP="${OMNIVOICE_NUM_STEP:-8}"
REQUEST_TIMEOUT_S="${OMNIVOICE_REQUEST_TIMEOUT_S:-120}"
STATE_DIR="${OMNIVOICE_STATE_DIR:-$SCRIPT_DIR/.state/omnivoice-server}"
PROFILE_DIR="${OMNIVOICE_PROFILE_DIR:-$STATE_DIR/profiles}"
MODEL_CACHE_DIR="${OMNIVOICE_MODEL_CACHE_DIR:-$STATE_DIR/hf-cache}"
XDG_CACHE_HOME_DIR="${OMNIVOICE_XDG_CACHE_HOME:-$STATE_DIR/xdg-cache}"
SERVER_LOG="${OMNIVOICE_SERVER_LOG:-/tmp/omnivoice_server.log}"
AUTO_CREATE_CAST_CLONES="${OMNIVOICE_AUTO_CREATE_CAST_CLONES:-1}"
CAST_CLONE_HELPER="${OMNIVOICE_CAST_CLONE_HELPER:-$SCRIPT_DIR/omnivoice_test/create_cast_clone_profiles.sh}"
GUIDANCE_SCALE="${OMNIVOICE_GUIDANCE_SCALE:-}"
DENOISE="${OMNIVOICE_DENOISE:-}"
T_SHIFT="${OMNIVOICE_T_SHIFT:-}"
POSITION_TEMPERATURE="${OMNIVOICE_POSITION_TEMPERATURE:-}"
CLASS_TEMPERATURE="${OMNIVOICE_CLASS_TEMPERATURE:-}"

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

CUDA_STATUS_JSON="$(
  "$VENV_PYTHON" - <<'PY'
import json
import torch

print(json.dumps({
    "cuda_available": bool(torch.cuda.is_available()),
    "cuda_device_count": int(torch.cuda.device_count()),
}))
PY
)"

CUDA_AVAILABLE="$("$VENV_PYTHON" - <<'PY'
import torch
print("1" if torch.cuda.is_available() else "0")
PY
)"

if [[ -z "$DEVICE" ]]; then
  if [[ "$CUDA_AVAILABLE" == "1" ]]; then
    DEVICE="cuda"
  else
    DEVICE="cpu"
  fi
fi

if [[ "$DEVICE" == "cuda" && "$CUDA_AVAILABLE" != "1" ]]; then
  echo "OMNIVOICE_DEVICE=cuda was requested, but torch cannot see a CUDA GPU." >&2
  echo "CUDA probe: $CUDA_STATUS_JSON" >&2
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

reclaim_port() {
  local port="$1"
  local pids
  mapfile -t pids < <(find_listener_pids "$port" | sed '/^$/d')
  if [[ "${#pids[@]}" -eq 0 ]]; then
    return
  fi

  echo "Port $port is busy; reclaiming it from PID(s): ${pids[*]}"
  for pid in "${pids[@]}"; do
    kill "$pid" >/dev/null 2>&1 || true
  done
  sleep 1

  mapfile -t pids < <(find_listener_pids "$port" | sed '/^$/d')
  for pid in "${pids[@]}"; do
    kill -9 "$pid" >/dev/null 2>&1 || true
  done
}

mkdir -p "$PROFILE_DIR" "$MODEL_CACHE_DIR" "$XDG_CACHE_HOME_DIR"
reclaim_port "$PORT"

echo "Starting omnivoice-server on $API_BASE"
echo "  model:   $MODEL_ID"
echo "  device:  $DEVICE"
echo "  torch:   $CUDA_STATUS_JSON"
echo "  profile: $PROFILE_DIR"
echo "  cache:   $MODEL_CACHE_DIR"
echo "  log:     $SERVER_LOG"
echo

: >"$SERVER_LOG"

SERVER_ENV=(
  "PATH=$PATH"
  "HOME=$HOME"
  "PYTHONPATH=$SERVER_DIR"
  "XDG_CACHE_HOME=$XDG_CACHE_HOME_DIR"
  "HF_HOME=$MODEL_CACHE_DIR"
  "HF_HUB_CACHE=$MODEL_CACHE_DIR/hub"
  "HUGGINGFACE_HUB_CACHE=$MODEL_CACHE_DIR/hub"
  "HF_HUB_DISABLE_XET=1"
  "OMNIVOICE_HOST=$HOST"
  "OMNIVOICE_PORT=$PORT"
  "OMNIVOICE_MODEL_ID=$MODEL_ID"
  "OMNIVOICE_MODEL_CACHE_DIR=$MODEL_CACHE_DIR"
  "OMNIVOICE_DEVICE=$DEVICE"
  "OMNIVOICE_NUM_STEP=$NUM_STEP"
  "OMNIVOICE_PROFILE_DIR=$PROFILE_DIR"
  "OMNIVOICE_REQUEST_TIMEOUT_S=$REQUEST_TIMEOUT_S"
)

if [[ -n "$GUIDANCE_SCALE" ]]; then
  SERVER_ENV+=("OMNIVOICE_GUIDANCE_SCALE=$GUIDANCE_SCALE")
fi
if [[ -n "$DENOISE" ]]; then
  SERVER_ENV+=("OMNIVOICE_DENOISE=$DENOISE")
fi
if [[ -n "$T_SHIFT" ]]; then
  SERVER_ENV+=("OMNIVOICE_T_SHIFT=$T_SHIFT")
fi
if [[ -n "$POSITION_TEMPERATURE" ]]; then
  SERVER_ENV+=("OMNIVOICE_POSITION_TEMPERATURE=$POSITION_TEMPERATURE")
fi
if [[ -n "$CLASS_TEMPERATURE" ]]; then
  SERVER_ENV+=("OMNIVOICE_CLASS_TEMPERATURE=$CLASS_TEMPERATURE")
fi

(
  cd "$SERVER_DIR"
  exec env -i \
    "${SERVER_ENV[@]}" \
    "$VENV_PYTHON" -m omnivoice_server.cli >>"$SERVER_LOG" 2>&1
) &
SERVER_PID="$!"

cleanup() {
  if [[ -n "${SERVER_PID:-}" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

echo "Waiting for server readiness..."
for _ in $(seq 1 300); do
  if curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
    break
  fi
  if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    echo "omnivoice-server exited before becoming healthy. Tail of log:" >&2
    tail -n 120 "$SERVER_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

if ! curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
  echo "Timed out waiting for $API_BASE/health" >&2
  tail -n 120 "$SERVER_LOG" >&2 || true
  exit 1
fi

echo "Server is ready."
curl -fsS "$API_BASE/health" || true
echo
echo

if [[ "$AUTO_CREATE_CAST_CLONES" == "1" ]]; then
  if [[ ! -x "$CAST_CLONE_HELPER" ]]; then
    echo "Clone helper is missing or not executable: $CAST_CLONE_HELPER" >&2
    exit 1
  fi
  echo "Ensuring built-in cast clone profiles..."
  if ! OMNIVOICE_API_BASE="$API_BASE" \
    OMNIVOICE_PROFILE_DIR="$PROFILE_DIR" \
    "$CAST_CLONE_HELPER"; then
    echo "Failed to ensure cast clone profiles." >&2
    exit 1
  fi
  echo
fi

echo "Leave this terminal open. Press Ctrl+C to stop the server."

wait "$SERVER_PID"
