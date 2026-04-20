#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

HOST="${OMNIVOICE_HOST:-${TTS_SERVER_HOST:-127.0.0.1}}"
PORT="${OMNIVOICE_PORT:-${TTS_SERVER_PORT:-8881}}"
API_BASE="${OMNIVOICE_API_BASE:-${TTS_API_BASE:-http://$HOST:$PORT}}"
VOICE_NAME="${OMNIVOICE_TEST_VOICE:-ballad}"
TEXT="${*:-This is a simple OmniVoice streaming test. The audio should start playing as PCM as soon as the first chunk arrives.}"
INSTRUCTIONS="${OMNIVOICE_TEST_INSTRUCTIONS:-}"
MODEL_NAME="${OMNIVOICE_TEST_MODEL_NAME:-tts-1}"
AUTO_CREATE_CAST_CLONES="${OMNIVOICE_AUTO_CREATE_CAST_CLONES:-1}"
CAST_CLONE_HELPER="${OMNIVOICE_CAST_CLONE_HELPER:-$ROOT_DIR/omnivoice_test/create_cast_clone_profiles.sh}"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on PATH" >&2
  exit 1
fi

if ! curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
  echo "omnivoice-server is not ready at $API_BASE" >&2
  exit 1
fi

if [[ "$AUTO_CREATE_CAST_CLONES" == "1" && -x "$CAST_CLONE_HELPER" ]]; then
  OMNIVOICE_API_BASE="$API_BASE" "$CAST_CLONE_HELPER" >/dev/null
fi

PLAYER_TYPE=""
if command -v play >/dev/null 2>&1; then
  PLAYER_TYPE="sox"
elif command -v ffplay >/dev/null 2>&1; then
  PLAYER_TYPE="ffplay"
else
  echo "Need either SoX 'play' or ffplay on PATH for audio playback." >&2
  exit 1
fi

REQUEST_JSON="$(
  TEXT="$TEXT" \
  VOICE_NAME="$VOICE_NAME" \
  INSTRUCTIONS="$INSTRUCTIONS" \
  MODEL_NAME="$MODEL_NAME" \
  "$VENV_PYTHON" - <<'PY'
import json
import os

payload = {
    "model": os.environ["MODEL_NAME"],
    "input": os.environ["TEXT"],
    "voice": os.environ["VOICE_NAME"],
    "stream": True,
    "response_format": "pcm",
}
if os.environ.get("INSTRUCTIONS"):
    payload["instructions"] = os.environ["INSTRUCTIONS"]
print(json.dumps(payload))
PY
)"

echo "Streaming from $API_BASE"
echo "Voice: $VOICE_NAME"
echo "Text:  $TEXT"
if [[ -n "$INSTRUCTIONS" ]]; then
  echo "Instructions: $INSTRUCTIONS"
fi
echo

if [[ "$PLAYER_TYPE" == "sox" ]]; then
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$REQUEST_JSON" \
    "$API_BASE/v1/audio/speech" \
    | play -q -t raw -b 16 -e signed-integer -c 1 -r 24000 -
else
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$REQUEST_JSON" \
    "$API_BASE/v1/audio/speech" \
    | ffplay -autoexit -nodisp -loglevel error -f s16le -ar 24000 -ac 1 -i pipe:0
fi
