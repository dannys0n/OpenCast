#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
OMNIVOICE_ENV_FILE="${OMNIVOICE_SERVER_ENV_FILE:-$ROOT_DIR/omnivoice-server/.env}"
OPENCAST_OMNIVOICE_ENV_FILE="${OPENCAST_OMNIVOICE_ENV_FILE:-$ROOT_DIR/omnivoice-server/.opencast.env}"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
VOICE_SOURCE_DIR="${OMNIVOICE_CAST_VOICE_SOURCE_DIR:-$ROOT_DIR/voices}"
STATE_DIR="${OMNIVOICE_CAST_STATE_DIR:-$ROOT_DIR/.state/omnivoice-cast-clones}"
TMP_DIR="$STATE_DIR/converted_refs"

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

ANNOUNCER_PROFILE_ID="${OMNIVOICE_ANNOUNCER_PROFILE_ID:-announcer_e0}"
ANNOUNCER_AUDIO_SOURCE="${OMNIVOICE_ANNOUNCER_AUDIO_SOURCE:-$VOICE_SOURCE_DIR/announcer E0.m4a}"
ANNOUNCER_TEXT_SOURCE="${OMNIVOICE_ANNOUNCER_TEXT_SOURCE:-$VOICE_SOURCE_DIR/announcer E0.txt}"

TURRET_PROFILE_ID="${OMNIVOICE_TURRET_PROFILE_ID:-turret_e0}"
TURRET_AUDIO_SOURCE="${OMNIVOICE_TURRET_AUDIO_SOURCE:-$VOICE_SOURCE_DIR/turret E0.m4a}"
TURRET_TEXT_SOURCE="${OMNIVOICE_TURRET_TEXT_SOURCE:-$VOICE_SOURCE_DIR/turret E0.txt}"

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "curl is required on PATH" >&2
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "ffmpeg is required on PATH for m4a -> wav conversion." >&2
  exit 1
fi

if ! curl -fsS "$API_BASE/health" >/dev/null 2>&1; then
  echo "omnivoice-server is not ready at $API_BASE" >&2
  exit 1
fi

mkdir -p "$TMP_DIR"

convert_ref_audio() {
  local source_path="$1"
  local output_path="$2"

  if [[ ! -f "$source_path" ]]; then
    echo "Missing reference audio: $source_path" >&2
    exit 1
  fi

  ffmpeg -y -loglevel error \
    -i "$source_path" \
    -vn \
    -ac 1 \
    -ar 24000 \
    -c:a pcm_s16le \
    "$output_path"
}

load_ref_text() {
  local text_path="$1"

  if [[ ! -f "$text_path" ]]; then
    echo "Missing reference transcript: $text_path" >&2
    exit 1
  fi

  TEXT_PATH="$text_path" "$VENV_PYTHON" - <<'PY'
from pathlib import Path
import os

text = Path(os.environ["TEXT_PATH"]).read_text(encoding="utf-8").strip()
print(" ".join(text.split()))
PY
}

create_profile() {
  local profile_id="$1"
  local source_audio="$2"
  local source_text="$3"
  local wav_path="$TMP_DIR/${profile_id}.wav"
  local ref_text

  echo
  echo "Preparing clone profile: $profile_id"
  echo "  audio: $source_audio"
  echo "  text:  $source_text"

  convert_ref_audio "$source_audio" "$wav_path"
  ref_text="$(load_ref_text "$source_text")"

  curl -fsS \
    -X POST \
    -F "profile_id=$profile_id" \
    -F "ref_text=$ref_text" \
    -F "overwrite=true" \
    -F "ref_audio=@${wav_path};type=audio/wav" \
    "$API_BASE/v1/voices/profiles" >/tmp/"${profile_id}"_create_profile.json

  echo "Profile uploaded: clone:$profile_id"
  curl -fsS "$API_BASE/v1/voices/profiles/$profile_id"
  echo
}

echo "Ensuring OmniVoice clone profiles at $API_BASE"
echo "Converted refs: $TMP_DIR"

create_profile "$ANNOUNCER_PROFILE_ID" "$ANNOUNCER_AUDIO_SOURCE" "$ANNOUNCER_TEXT_SOURCE"
create_profile "$TURRET_PROFILE_ID" "$TURRET_AUDIO_SOURCE" "$TURRET_TEXT_SOURCE"

echo
echo "Available clone voices:"
VOICES_JSON="$(curl -fsS "$API_BASE/v1/voices")"
VOICES_JSON="$VOICES_JSON" "$VENV_PYTHON" - <<'PY'
import json
import os

payload = json.loads(os.environ["VOICES_JSON"])
for voice in payload.get("voices", []):
    if voice.get("type") == "clone":
        print(f"- {voice.get('id')}")
PY
