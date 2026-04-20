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
MODEL_NAME="${OMNIVOICE_TEST_MODEL_NAME:-tts-1}"
AUTO_CREATE_CAST_CLONES="${OMNIVOICE_AUTO_CREATE_CAST_CLONES:-1}"
CAST_CLONE_HELPER="${OMNIVOICE_CAST_CLONE_HELPER:-$ROOT_DIR/omnivoice_test/create_cast_clone_profiles.sh}"

ANNOUNCER_PROFILE_ID="${OMNIVOICE_ANNOUNCER_PROFILE_ID:-announcer_e0}"
TURRET_PROFILE_ID="${OMNIVOICE_TURRET_PROFILE_ID:-turret_e0}"

ANNOUNCER_TEXT="${OMNIVOICE_ANNOUNCER_TEXT:-Round timer still favors the defense. Mid pressure can still break that open.}"
TURRET_TEXT="${OMNIVOICE_TURRET_TEXT:-Oh. That looks a little dangerous. The B anchor still has too much to cover.}"

SELECTOR="${1:-both}"
if [[ "$#" -gt 0 ]]; then
  shift
fi
CUSTOM_TEXT="${*:-}"

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

stream_voice() {
  local voice_name="$1"
  local text="$2"

  local request_json
  request_json="$(
    TEXT="$text" \
    VOICE_NAME="$voice_name" \
    MODEL_NAME="$MODEL_NAME" \
    "$VENV_PYTHON" - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_NAME"],
    "input": os.environ["TEXT"],
    "voice": os.environ["VOICE_NAME"],
    "stream": True,
    "response_format": "pcm",
}))
PY
  )"

  echo
  echo "Streaming $voice_name"
  echo "Text: $text"

  if [[ "$PLAYER_TYPE" == "sox" ]]; then
    curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "$API_BASE/v1/audio/speech" \
      | play -q -t raw -b 16 -e signed-integer -c 1 -r 24000 -
  else
    curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "$API_BASE/v1/audio/speech" \
      | ffplay -autoexit -nodisp -loglevel error -f s16le -ar 24000 -ac 1 -i pipe:0
  fi
}

case "$SELECTOR" in
  announcer)
    stream_voice "clone:$ANNOUNCER_PROFILE_ID" "${CUSTOM_TEXT:-$ANNOUNCER_TEXT}"
    ;;
  turret)
    stream_voice "clone:$TURRET_PROFILE_ID" "${CUSTOM_TEXT:-$TURRET_TEXT}"
    ;;
  both)
    stream_voice "clone:$ANNOUNCER_PROFILE_ID" "${OMNIVOICE_ANNOUNCER_TEXT:-$ANNOUNCER_TEXT}"
    stream_voice "clone:$TURRET_PROFILE_ID" "${OMNIVOICE_TURRET_TEXT:-$TURRET_TEXT}"
    ;;
  *)
    echo "Usage: $(basename "$0") [announcer|turret|both] [custom text]" >&2
    exit 1
    ;;
esac
