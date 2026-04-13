#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

TTS_API_BASE="${TTS_API_BASE:-http://127.0.0.1:8880}"
VOICE_NAME="${VOICE_NAME:-clone:announcer_e0}"
TEXT="${*:-This is a simple TTS prompt test.}"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
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

if ! curl -fsS "$TTS_API_BASE/health" >/dev/null 2>&1; then
  echo "TTS server is not ready at $TTS_API_BASE" >&2
  exit 1
fi

REQUEST_JSON="$(
  VOICE_NAME="$VOICE_NAME" TEXT="$TEXT" "$VENV_PYTHON" - <<'PY'
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

echo "Prompting $VOICE_NAME"
echo "Text: $TEXT"

curl -fsS \
  -H "Content-Type: application/json" \
  -d "$REQUEST_JSON" \
  "$TTS_API_BASE/v1/audio/speech" \
  | play -q -t raw -b 16 -e signed-integer -c 1 -r 24000 -
