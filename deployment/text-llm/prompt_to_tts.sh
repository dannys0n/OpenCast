#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"

MODEL_API_BASE_DEFAULT="http://127.0.0.1:12434"
MODEL_NAME_DEFAULT="hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
SYSTEM_PROMPT_DEFAULT="You are an esports commentator. Respond with short, high-energy commentary sentences only. No markdown. No lists. No reasoning."
DEFAULT_PROMPT_DEFAULT="Give me one short, high-energy esports caster line for a team wipe."
SERVER_URL_DEFAULT="ws://localhost:8091/v1/audio/speech/stream"
TEMPERATURE_DEFAULT="0.4"
MAX_TOKENS_DEFAULT="160"

if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

MODEL_API_BASE="${MODEL_API_BASE:-$MODEL_API_BASE_DEFAULT}"
MODEL_NAME="${MODEL_NAME:-$MODEL_NAME_DEFAULT}"
SYSTEM_PROMPT="${SYSTEM_PROMPT:-$SYSTEM_PROMPT_DEFAULT}"
DEFAULT_PROMPT="${DEFAULT_PROMPT:-$DEFAULT_PROMPT_DEFAULT}"
SERVER_URL="${SERVER_URL:-$SERVER_URL_DEFAULT}"
TEMPERATURE="${TEMPERATURE:-$TEMPERATURE_DEFAULT}"
MAX_TOKENS="${MAX_TOKENS:-$MAX_TOKENS_DEFAULT}"
PROMPT_TEXT="${*:-$DEFAULT_PROMPT}"
VOICE_CONFIG_FILE="${VOICE_CONFIG_FILE:-$REPO_ROOT/tts-io/custom_voice.env}"

if ! command -v curl >/dev/null 2>&1; then
  echo "Missing curl."
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Missing jq."
  exit 1
fi

if [ ! -d "$REPO_ROOT/.venv" ]; then
  echo "Missing .venv. Run sh tts-io/setup_linux_env.sh first."
  exit 1
fi

if [ ! -f "$VOICE_CONFIG_FILE" ]; then
  echo "Missing custom voice config: $VOICE_CONFIG_FILE"
  echo "Run: sh tts-io/add_custom_voice.sh"
  exit 1
fi

cd "$REPO_ROOT"
source .venv/bin/activate
source "$VOICE_CONFIG_FILE"

if [ ! -f "$CUSTOM_VOICE_EMBEDDING_FILE" ]; then
  echo "Missing custom voice embedding file: $CUSTOM_VOICE_EMBEDDING_FILE"
  echo "Run: sh tts-io/add_custom_voice.sh"
  exit 1
fi

request_payload="$(
  jq -nc \
    --arg model "$MODEL_NAME" \
    --arg system "$SYSTEM_PROMPT" \
    --arg prompt "$PROMPT_TEXT" \
    --argjson temperature "$TEMPERATURE" \
    --argjson max_tokens "$MAX_TOKENS" \
    '{
      model: $model,
      messages: [
        {role: "system", content: $system},
        {role: "user", content: $prompt}
      ],
      temperature: $temperature,
      max_tokens: $max_tokens,
      stream: true
    }'
)"

tts_cmd=(
  python "$REPO_ROOT/tts-io/stream_tts.py"
  --url "$SERVER_URL"
  --stdin-chunks
  --speaker-embedding-file "$CUSTOM_VOICE_EMBEDDING_FILE"
)

echo "Prompting text model and streaming chunks into TTS..."
echo

parse_sse_chunks() {
  while IFS= read -r line; do
    case "$line" in
      "data: [DONE]")
        break
        ;;
      data:\ *)
        json_payload="${line#data: }"
        content="$(printf '%s' "$json_payload" | jq -r '.choices[0].delta.content // empty')"
        if [ -n "$content" ]; then
          printf '%s\n' "$content"
        fi
        ;;
    esac
  done
}

curl -sN "$MODEL_API_BASE/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d "$request_payload" \
  | parse_sse_chunks \
  | "${tts_cmd[@]}"
