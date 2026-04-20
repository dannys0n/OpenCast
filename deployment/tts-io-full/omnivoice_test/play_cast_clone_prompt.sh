#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
OMNIVOICE_ENV_FILE="${OMNIVOICE_SERVER_ENV_FILE:-$ROOT_DIR/omnivoice-server/.env}"
OPENCAST_OMNIVOICE_ENV_FILE="${OPENCAST_OMNIVOICE_ENV_FILE:-$ROOT_DIR/omnivoice-server/.opencast.env}"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"

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

if [[ -f "$OPENCAST_OMNIVOICE_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OPENCAST_OMNIVOICE_ENV_FILE"
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

ANNOUNCER_TEXT="${OMNIVOICE_ANNOUNCER_TEXT:-Great work! Because this message is prerecorded, any observations related to your performance are speculation on our part. Please disregard any undeserved compliments.}"
TURRET_TEXT="${OMNIVOICE_TURRET_TEXT:-Prometheus was punished by the gods for giving the gift of knowledge to man. He was cast into the bowels of the earth and pecked by birds.}"

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

def env_bool(name):
    value = os.environ.get(name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None

payload = {
    "model": os.environ["MODEL_NAME"],
    "input": os.environ["TEXT"],
    "voice": os.environ["VOICE_NAME"],
    "stream": True,
    "response_format": "pcm",
}
optional = {
    "num_step": int(os.environ["OMNIVOICE_TTS_NUM_STEP"]) if os.environ.get("OMNIVOICE_TTS_NUM_STEP") else None,
    "guidance_scale": float(os.environ["OMNIVOICE_TTS_GUIDANCE_SCALE"]) if os.environ.get("OMNIVOICE_TTS_GUIDANCE_SCALE") else None,
    "denoise": env_bool("OMNIVOICE_TTS_DENOISE"),
    "t_shift": float(os.environ["OMNIVOICE_TTS_T_SHIFT"]) if os.environ.get("OMNIVOICE_TTS_T_SHIFT") else None,
    "position_temperature": float(os.environ["OMNIVOICE_TTS_POSITION_TEMPERATURE"]) if os.environ.get("OMNIVOICE_TTS_POSITION_TEMPERATURE") else None,
    "class_temperature": float(os.environ["OMNIVOICE_TTS_CLASS_TEMPERATURE"]) if os.environ.get("OMNIVOICE_TTS_CLASS_TEMPERATURE") else None,
    "duration": float(os.environ["OMNIVOICE_TTS_DURATION"]) if os.environ.get("OMNIVOICE_TTS_DURATION") else None,
    "language": os.environ.get("OMNIVOICE_TTS_LANGUAGE") or None,
    "layer_penalty_factor": float(os.environ["OMNIVOICE_TTS_LAYER_PENALTY_FACTOR"]) if os.environ.get("OMNIVOICE_TTS_LAYER_PENALTY_FACTOR") else None,
    "preprocess_prompt": env_bool("OMNIVOICE_TTS_PREPROCESS_PROMPT"),
    "postprocess_output": env_bool("OMNIVOICE_TTS_POSTPROCESS_OUTPUT"),
    "audio_chunk_duration": float(os.environ["OMNIVOICE_TTS_AUDIO_CHUNK_DURATION"]) if os.environ.get("OMNIVOICE_TTS_AUDIO_CHUNK_DURATION") else None,
    "audio_chunk_threshold": float(os.environ["OMNIVOICE_TTS_AUDIO_CHUNK_THRESHOLD"]) if os.environ.get("OMNIVOICE_TTS_AUDIO_CHUNK_THRESHOLD") else None,
}
payload.update({k: v for k, v in optional.items() if v is not None})
print(json.dumps(payload))
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
