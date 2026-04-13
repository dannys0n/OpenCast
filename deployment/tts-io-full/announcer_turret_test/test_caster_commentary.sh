#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PROJECT_DIR="$ROOT_DIR/Qwen3-TTS-Openai-Fastapi"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
TEXT_LLM_ENV_FILE="$ROOT_DIR/../text-llm/.env"

if [[ -f "$TEXT_LLM_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$TEXT_LLM_ENV_FILE"
  set +a
fi

# Edit these values directly for quick local testing.
CONFIG_FILE="$PROJECT_DIR/config.opencast.local.yaml"
VOICE_LIBRARY_DIR="$PROJECT_DIR/voice_library"
HOST="127.0.0.1"
PORT="8880"
SERVER_LOG="/tmp/qwen3_tts_openai_fastapi_caster_commentary.log"
TMP_ROOT="/tmp"

MODEL_API_BASE="${MODEL_API_BASE:-http://127.0.0.1:12434}"
MODEL_NAME="${MODEL_NAME:-hf.co/unsloth/Qwen3-1.7B-GGUF:Q4_K_M}"
MODEL_NAME="${MODEL_NAME%/no_think}"
MODEL_TEMPERATURE="0.7"
MODEL_MAX_TOKENS="220"
MODEL_TIMEOUT="45"
LINE_COUNT="4"

VOICE_NAME="clone:announcer_e0"
SAMPLE_RATE="24000"
TTS_SPEED="1.12"
TTS_INSTRUCT="Deliver it as rapid play-by-play commentary. Speak with energetic excitement and forward momentum."
SCENARIO_TEXT="On Mirage in a packed arena, the star rifler cracks open A with two instant headshots, the lurker catches the rotate through connector, and the last defender is denied on the smoke defuse as the crowd erupts."

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Missing project dir: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
  echo "Missing local venv python at $VENV_PYTHON" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "Missing config file: $CONFIG_FILE" >&2
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

if ! command -v play >/dev/null 2>&1; then
  echo "SoX 'play' is required on PATH" >&2
  exit 1
fi

if ! command -v mktemp >/dev/null 2>&1; then
  echo "mktemp is required on PATH" >&2
  exit 1
fi

SERVER_PID=""
PLAY_PID=""
TEMP_DIR=""
CURL_PIDS=()

cleanup() {
  local pid

  if [[ -n "$PLAY_PID" ]] && kill -0 "$PLAY_PID" >/dev/null 2>&1; then
    kill "$PLAY_PID" >/dev/null 2>&1 || true
    wait "$PLAY_PID" >/dev/null 2>&1 || true
  fi

  for pid in "${CURL_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}

trap cleanup EXIT

echo "Requesting ${LINE_COUNT} short caster lines from $MODEL_API_BASE ..."
COMMENTARY_JSON="$(
  MODEL_API_BASE="$MODEL_API_BASE" \
  MODEL_NAME="$MODEL_NAME" \
  MODEL_TEMPERATURE="$MODEL_TEMPERATURE" \
  MODEL_MAX_TOKENS="$MODEL_MAX_TOKENS" \
  MODEL_TIMEOUT="$MODEL_TIMEOUT" \
  LINE_COUNT="$LINE_COUNT" \
  SCENARIO_TEXT="$SCENARIO_TEXT" \
  "$VENV_PYTHON" - <<'PY'
import json
import os
import re
import urllib.error
import urllib.request


def extract_message_content(response_json):
    choices = response_json.get("choices") or []
    if not choices:
        raise RuntimeError("text model returned no choices")

    message = choices[0].get("message") or {}
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("text model returned empty content")
    content = content.strip()
    content = re.sub(r"<think>.*?</think>\s*", "", content, flags=re.DOTALL).strip()
    return content


def extract_json_object(raw_text):
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    start = raw_text.find("{")
    end = raw_text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise RuntimeError("text model response did not contain a JSON object")

    return json.loads(raw_text[start : end + 1])


model_api_base = os.environ["MODEL_API_BASE"].rstrip("/")
model_name = os.environ["MODEL_NAME"]
temperature = float(os.environ["MODEL_TEMPERATURE"])
max_tokens = int(os.environ["MODEL_MAX_TOKENS"])
timeout_seconds = float(os.environ["MODEL_TIMEOUT"])
line_count = max(1, int(os.environ["LINE_COUNT"]))
scenario_text = os.environ["SCENARIO_TEXT"]

system_prompt = (
    "You are an elite Counter-Strike 2 play-by-play caster. "
    "Return JSON only. "
    "Use exactly one top-level key named lines. "
    "lines must be an array of short spoken sentences for live commentary. "
    "Each sentence must be punchy, natural to say aloud, and under 12 words. "
    "No numbering. No markdown. No labels. No code fences."
)

user_prompt = (
    f"Generate {line_count} short commentary sentences for this single highlight sequence.\n\n"
    "The lines should feel like consecutive live calls during one exciting high-level CS2 play.\n"
    "Keep them varied, escalating, and immediately speakable.\n\n"
    f"Scenario:\n{scenario_text}\n\n"
    "Return JSON like:\n"
    '{\n  "lines": ["...", "..."]\n}'
)
user_prompt = user_prompt.rstrip() + "\n/no_think"

request_body = {
    "model": model_name,
    "messages": [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ],
    "temperature": temperature,
    "max_tokens": max_tokens,
    "stream": False,
}

request = urllib.request.Request(
    f"{model_api_base}/v1/chat/completions",
    data=json.dumps(request_body).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response_json = json.loads(response.read().decode("utf-8"))
except urllib.error.HTTPError as error:
    body = error.read().decode("utf-8", errors="replace")
    raise RuntimeError(f"text model HTTP {error.code}: {body}") from error
except urllib.error.URLError as error:
    raise RuntimeError(f"text model request failed: {error}") from error

raw_text = extract_message_content(response_json)
parsed = extract_json_object(raw_text)
lines = parsed.get("lines")
if not isinstance(lines, list):
    raise RuntimeError("text model JSON did not include a lines array")

cleaned_lines = []
for value in lines:
    if not isinstance(value, str):
        continue
    line = " ".join(value.strip().split())
    if line:
        cleaned_lines.append(line)
    if len(cleaned_lines) >= line_count:
        break

if not cleaned_lines:
    raise RuntimeError("text model returned no usable commentary lines")

print(json.dumps({"lines": cleaned_lines}))
PY
)"

mapfile -t TEXTS < <(
  COMMENTARY_JSON="$COMMENTARY_JSON" "$VENV_PYTHON" - <<'PY'
import json
import os

payload = json.loads(os.environ["COMMENTARY_JSON"])
for line in payload["lines"]:
    print(line)
PY
)

if [[ "${#TEXTS[@]}" -eq 0 ]]; then
  echo "No commentary lines were generated." >&2
  exit 1
fi

echo "Generated commentary lines:"
for i in "${!TEXTS[@]}"; do
  printf '  %s. %s\n' "$((i + 1))" "${TEXTS[$i]}"
done

echo
echo "Starting optimized FastAPI TTS server..."
(
  cd "$PROJECT_DIR"
  export PYTHONPATH="$PROJECT_DIR:${PYTHONPATH:-}"
  export TTS_BACKEND="optimized"
  export TTS_CONFIG="$CONFIG_FILE"
  export VOICE_LIBRARY_DIR="$VOICE_LIBRARY_DIR"
  export HOST="$HOST"
  export PORT="$PORT"
  exec "$VENV_PYTHON" -m api.main
) >"$SERVER_LOG" 2>&1 &
SERVER_PID="$!"

echo "Waiting for server on http://$HOST:$PORT ..."
for _ in $(seq 1 120); do
  if curl -fsS "http://$HOST:$PORT/v1/voices" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done

if ! curl -fsS "http://$HOST:$PORT/v1/voices" >/dev/null 2>&1; then
  echo "Server failed to become ready. Recent log:" >&2
  tail -n 120 "$SERVER_LOG" >&2 || true
  exit 1
fi

TEMP_DIR="$(mktemp -d "$TMP_ROOT/qwen3_tts_caster_commentary.XXXXXX")"
AGGREGATE_FIFO="$TEMP_DIR/sequence.pcm"
mkfifo "$AGGREGATE_FIFO"

declare -a BUFFER_FILES=()
declare -a DONE_FILES=()
declare -a STATUS_FILES=()

build_tts_request_json() {
  local voice_name="$1"
  local text="$2"

  VOICE_NAME="$voice_name" \
  TEXT="$text" \
  TTS_INSTRUCT="$TTS_INSTRUCT" \
  TTS_SPEED="$TTS_SPEED" \
  "$VENV_PYTHON" - <<'PY'
import json
import os

print(json.dumps({
    "model": "tts-1",
    "voice": os.environ["VOICE_NAME"],
    "input": os.environ["TEXT"],
    "instruct": os.environ["TTS_INSTRUCT"],
    "speed": float(os.environ["TTS_SPEED"]),
    "stream": True,
    "response_format": "pcm",
}))
PY
}

dispatch_request() {
  local index="$1"
  local voice_name="$2"
  local text="$3"
  local buffer_file="$TEMP_DIR/request_${index}.pcm"
  local done_file="$TEMP_DIR/request_${index}.done"
  local status_file="$TEMP_DIR/request_${index}.status"
  local request_json

  request_json="$(build_tts_request_json "$voice_name" "$text")"
  : >"$buffer_file"
  BUFFER_FILES+=("$buffer_file")
  DONE_FILES+=("$done_file")
  STATUS_FILES+=("$status_file")

  echo "Dispatching line $((index + 1)) for queued playback"
  (
    status=0
    if curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "http://$HOST:$PORT/v1/audio/speech" \
      >"$buffer_file"; then
      status=0
    else
      status=$?
    fi
    printf '%s\n' "$status" >"$status_file"
    touch "$done_file"
    exit "$status"
  ) &
  CURL_PIDS+=("$!")
}

stream_buffer_into_fd() {
  local buffer_file="$1"
  local done_file="$2"
  local status_file="$3"
  local offset=0
  local size=0
  local to_copy=0
  local status=0

  while :; do
    size="$(stat -c '%s' "$buffer_file" 2>/dev/null || printf '0')"
    if (( size > offset )); then
      to_copy=$((size - offset))
      dd if="$buffer_file" bs=1 skip="$offset" count="$to_copy" status=none >&3
      offset="$size"
      continue
    fi

    if [[ -f "$done_file" ]]; then
      if [[ -f "$status_file" ]]; then
        status="$(<"$status_file")"
      fi
      if [[ "$status" != "0" ]]; then
        echo "Request failed while streaming $buffer_file" >&2
        return 1
      fi
      break
    fi

    sleep 0.01
  done
}

echo "Starting seamless playback pipeline..."
play -q -t raw -b 16 -e signed-integer -c 1 -r "$SAMPLE_RATE" "$AGGREGATE_FIFO" &
PLAY_PID="$!"

exec 3>"$AGGREGATE_FIFO"

for i in "${!TEXTS[@]}"; do
  dispatch_request "$i" "$VOICE_NAME" "${TEXTS[$i]}"
done

echo
echo "All commentary requests dispatched. Streaming them in order through one playback session..."

for i in "${!BUFFER_FILES[@]}"; do
  printf 'Queueing %s: %s\n' "$((i + 1))" "${TEXTS[$i]}"
  stream_buffer_into_fd "${BUFFER_FILES[$i]}" "${DONE_FILES[$i]}" "${STATUS_FILES[$i]}"
done

exec 3>&-

for pid in "${CURL_PIDS[@]}"; do
  wait "$pid"
done

wait "$PLAY_PID"

echo
echo "Caster commentary playback finished."
