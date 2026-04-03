#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VOICES_DIR="${VOICES_DIR:-$SCRIPT_DIR/voices}"
GENERATED_DIR="$VOICES_DIR/generated"
NORMALIZED_DIR="$GENERATED_DIR/normalized"
EMBEDDINGS_DIR="$GENERATED_DIR/embeddings"
ENV_DIR="$GENERATED_DIR/env"
MANIFEST_FILE="$GENERATED_DIR/voices.json"
DEFAULT_ENV_FILE="$GENERATED_DIR/default.env"
DEFAULT_VOICE_NAME="${DEFAULT_VOICE_NAME:-}"
ENV_FILE="$SCRIPT_DIR/.env"
TTS_MODEL_NAME="${TTS_MODEL_NAME:-Qwen/Qwen3-TTS-12Hz-0.6B-Base}"

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
  TTS_MODEL_NAME="${TTS_MODEL_NAME:-$TTS_MODEL_NAME}"
fi

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  echo "Missing .venv. Run sh tts-io/setup_linux_env.sh first."
  exit 1
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Missing ffmpeg. Install it first."
  exit 1
fi

case "$TTS_MODEL_NAME" in
  *-Base) ;;
  *)
    echo "TTS_MODEL_NAME must point to a Qwen3-TTS Base model for voice cloning."
    echo "Current value: $TTS_MODEL_NAME"
    exit 1
    ;;
esac

mkdir -p "$VOICES_DIR" "$NORMALIZED_DIR" "$EMBEDDINGS_DIR" "$ENV_DIR"
shopt -s nullglob

sanitize_voice_name() {
  local raw_name="$1"
  printf '%s' "$raw_name" \
    | tr '[:upper:]' '[:lower:]' \
    | sed -E 's/[^a-z0-9]+/_/g; s/^_+//; s/_+$//; s/__+/_/g'
}

convert_to_compatible_wav() {
  local source_file="$1"
  local target_file="$2"
  ffmpeg -loglevel error -y -i "$source_file" -ac 1 -ar 24000 -c:a pcm_s16le "$target_file"
}

voice_sources=(
  "$VOICES_DIR"/*.wav
  "$VOICES_DIR"/*.mp3
  "$VOICES_DIR"/*.m4a
  "$VOICES_DIR"/*.flac
  "$VOICES_DIR"/*.ogg
  "$VOICES_DIR"/*.opus
  "$VOICES_DIR"/*.webm
  "$VOICES_DIR"/*.aac
)

if [ "${#voice_sources[@]}" -eq 0 ]; then
  echo "No voice files found."
  echo "Place audio files under: $VOICES_DIR"
  echo "Supported formats: wav mp3 m4a flac ogg opus webm aac"
  exit 1
fi

manifest_lines=()
default_voice_env=""
default_voice_name=""
declare -A seen_voice_names=()

for source_file in "${voice_sources[@]}"; do
  source_path="$(readlink -f "$source_file")"
  voice_stem="$(basename "${source_path%.*}")"
  voice_name="$(sanitize_voice_name "$voice_stem")"

  if [ -z "$voice_name" ]; then
    echo "Could not derive a valid voice name from: $source_file"
    exit 1
  fi
  if [ -n "${seen_voice_names[$voice_name]:-}" ]; then
    echo "Duplicate voice name '$voice_name' derived from multiple files."
    echo "Rename the source files in $VOICES_DIR so each voice name is unique."
    exit 1
  fi
  seen_voice_names[$voice_name]=1

  normalized_file="$NORMALIZED_DIR/${voice_name}.wav"
  embedding_file="$EMBEDDINGS_DIR/${voice_name}.json"
  voice_env_file="$ENV_DIR/${voice_name}.env"

  echo "Preparing voice '$voice_name' from: $source_path"
  convert_to_compatible_wav "$source_path" "$normalized_file"

  "$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/make_speaker_embedding.py" \
    "$normalized_file" \
    --voice-name "$voice_name" \
    --source-file "$source_path" \
    --model-name "$TTS_MODEL_NAME" \
    > "$embedding_file"

  cat > "$voice_env_file" <<EOF
CUSTOM_VOICE_NAME="$voice_name"
CUSTOM_VOICE_SOURCE_FILE="$source_path"
CUSTOM_VOICE_FILE="$normalized_file"
CUSTOM_VOICE_EMBEDDING_FILE="$embedding_file"
EOF

  manifest_lines+=("${voice_name}"$'\t'"${source_path}"$'\t'"${normalized_file}"$'\t'"${embedding_file}"$'\t'"${voice_env_file}")

  if [ -z "$default_voice_env" ]; then
    default_voice_env="$voice_env_file"
    default_voice_name="$voice_name"
  fi
  if [ -n "$DEFAULT_VOICE_NAME" ] && [ "$voice_name" = "$DEFAULT_VOICE_NAME" ]; then
    default_voice_env="$voice_env_file"
    default_voice_name="$voice_name"
  fi
done

if [ -z "$default_voice_env" ]; then
  echo "Failed to select a default voice."
  exit 1
fi

"$REPO_ROOT/.venv/bin/python" - "$MANIFEST_FILE" "$default_voice_name" "$default_voice_env" "${manifest_lines[@]}" <<'PY'
import json
import sys
from pathlib import Path

manifest_path = Path(sys.argv[1])
default_voice_name = sys.argv[2]
default_env_file = sys.argv[3]
entries = []
for raw in sys.argv[4:]:
    name, source_file, normalized_file, embedding_file, env_file = raw.split("\t")
    entries.append(
        {
            "name": name,
            "source_file": source_file,
            "normalized_file": normalized_file,
            "embedding_file": embedding_file,
            "env_file": env_file,
        }
    )

manifest_path.write_text(
    json.dumps(
        {
            "default_voice_name": default_voice_name,
            "default_env_file": default_env_file,
            "voices": entries,
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

source "$default_voice_env"
cat > "$DEFAULT_ENV_FILE" <<EOF
CUSTOM_VOICE_NAME="$CUSTOM_VOICE_NAME"
CUSTOM_VOICE_SOURCE_FILE="$CUSTOM_VOICE_SOURCE_FILE"
CUSTOM_VOICE_FILE="$CUSTOM_VOICE_FILE"
CUSTOM_VOICE_EMBEDDING_FILE="$CUSTOM_VOICE_EMBEDDING_FILE"
CUSTOM_VOICES_DIR="$VOICES_DIR"
CUSTOM_VOICES_MANIFEST_FILE="$MANIFEST_FILE"
EOF

echo "Prepared ${#voice_sources[@]} voice(s)"
echo "Default voice        : $CUSTOM_VOICE_NAME"
echo "TTS Base model       : $TTS_MODEL_NAME"
echo "Saved default env to : $DEFAULT_ENV_FILE"
echo "Saved manifest to    : $MANIFEST_FILE"
echo "Saved per-voice envs : $ENV_DIR"
echo "Using Base + x-vector-only with a local speaker embedding."
