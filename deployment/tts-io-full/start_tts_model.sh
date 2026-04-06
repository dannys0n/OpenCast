#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
PROJECT_DIR="$SCRIPT_DIR/Qwen3-TTS-Openai-Fastapi"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

HOST="${TTS_SERVER_HOST:-127.0.0.1}"
PORT="${TTS_SERVER_PORT:-8880}"
TTS_API_BASE="${TTS_API_BASE:-http://127.0.0.1:8880}"
TTS_BACKEND="${TTS_BACKEND:-optimized}"
TTS_CONFIG="${TTS_CONFIG:-$PROJECT_DIR/config.opencast.local.yaml}"
VOICE_LIBRARY_DIR="${VOICE_LIBRARY_DIR:-$PROJECT_DIR/voice_library}"
TTS_WARMUP_ON_START="${TTS_WARMUP_ON_START:-true}"
TTS_PRELOAD_ALL_VOICES="${TTS_PRELOAD_ALL_VOICES:-1}"
TTS_PRELOAD_TEXT="${TTS_PRELOAD_TEXT:-This is a short warmup line for cached voice loading.}"

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Missing project dir: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if [[ ! -f "$TTS_CONFIG" ]]; then
  echo "Missing TTS config file: $TTS_CONFIG" >&2
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

find_listener_pids() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -ti "TCP:${port}" -sTCP:LISTEN 2>/dev/null || true
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    fuser "${port}/tcp" 2>/dev/null | tr ' ' '\n' || true
    return
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

SERVER_PID=""

cleanup() {
  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

reclaim_port "$PORT"

echo "Starting persistent TTS server on http://$HOST:$PORT"
echo "Backend:          $TTS_BACKEND"
echo "Config:           $TTS_CONFIG"
echo "Voice library:    $VOICE_LIBRARY_DIR"
echo "Warmup on start:  $TTS_WARMUP_ON_START"
echo

(
  cd "$PROJECT_DIR"
  export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"
  export HOST="$HOST"
  export PORT="$PORT"
  export TTS_BACKEND="$TTS_BACKEND"
  export TTS_CONFIG="$TTS_CONFIG"
  export VOICE_LIBRARY_DIR="$VOICE_LIBRARY_DIR"
  export TTS_WARMUP_ON_START="$TTS_WARMUP_ON_START"
  exec "$VENV_PYTHON" -m api.main
) &
SERVER_PID="$!"

echo "Waiting for server readiness..."
for _ in $(seq 1 120); do
  if curl -fsS "$TTS_API_BASE/health" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "$TTS_API_BASE/health" >/dev/null 2>&1; then
  echo "TTS server failed to become ready." >&2
  exit 1
fi

echo "Server is ready."

if [[ "$TTS_PRELOAD_ALL_VOICES" == "1" ]]; then
  echo "Preloading clone voices into cache..."
  "$VENV_PYTHON" - <<'PY' | while IFS= read -r voice_name; do
import json
import os
from pathlib import Path

profiles_dir = Path(os.environ["VOICE_LIBRARY_DIR"]) / "profiles"
if not profiles_dir.exists():
    raise SystemExit(0)

for meta_path in sorted(profiles_dir.glob("*/meta.json")):
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        continue
    name = (data.get("name") or meta_path.parent.name).strip()
    if name:
        print(f"clone:{name}")
PY
    [[ -n "$voice_name" ]] || continue
    echo "  warming $voice_name"
    request_json="$(
      VOICE_NAME="$voice_name" TEXT="$TTS_PRELOAD_TEXT" "$VENV_PYTHON" - <<'PY'
import json
import os

print(json.dumps({
    "model": "tts-1",
    "voice": os.environ["VOICE_NAME"],
    "input": os.environ["TEXT"],
    "stream": False,
    "response_format": "wav",
}))
PY
    )"
    if ! curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "$TTS_API_BASE/v1/audio/speech" \
      >/dev/null; then
      echo "  warning: failed to warm $voice_name" >&2
    fi
  done
fi

echo
echo "Persistent TTS server is running."
echo "Voices:"
curl -fsS "$TTS_API_BASE/v1/voices" || true
echo
echo
echo "Leave this terminal open. Press Ctrl+C to stop the server."

wait "$SERVER_PID"
