#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VOICE_NAME="${VOICE_NAME:-scotty}"
VOICE_FILE="${VOICE_FILE:-$SCRIPT_DIR/scotty_full.wav}"
VOICE_CONFIG_FILE="$SCRIPT_DIR/custom_voice.env"
VOICE_EMBEDDING_FILE="$SCRIPT_DIR/custom_voice_embedding.json"

if [ ! -f "$VOICE_FILE" ]; then
  echo "Missing voice file: $VOICE_FILE"
  exit 1
fi

if [ ! -x "$REPO_ROOT/.venv/bin/python" ]; then
  echo "Missing .venv. Run sh tts-io/setup_linux_env.sh first."
  exit 1
fi

"$REPO_ROOT/.venv/bin/python" "$SCRIPT_DIR/make_speaker_embedding.py" "$VOICE_FILE" > "$VOICE_EMBEDDING_FILE"

cat > "$VOICE_CONFIG_FILE" <<EOF
CUSTOM_VOICE_NAME="$VOICE_NAME"
CUSTOM_VOICE_FILE="$VOICE_FILE"
CUSTOM_VOICE_EMBEDDING_FILE="$VOICE_EMBEDDING_FILE"
EOF

echo "Prepared custom voice: $VOICE_NAME"
echo "Saved voice config to: $VOICE_CONFIG_FILE"
echo "Saved speaker embedding to: $VOICE_EMBEDDING_FILE"
echo "Using Base + x-vector-only with a local speaker embedding."
