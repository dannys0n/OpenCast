#!/usr/bin/env bash

[ -n "${BASH_VERSION:-}" ] || exec bash "$0" "$@"

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT_DIR/.env"
TEXT_LLM_ENV_FILE="${TEXT_LLM_ENV_FILE:-$ROOT_DIR/../text-llm/.env}"
VENV_PYTHON="$ROOT_DIR/.venv/bin/python"
START_SCRIPT="${OMNIVOICE_START_SCRIPT:-$ROOT_DIR/start_omnivoice_model.sh}"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$ENV_FILE"
  set +a
fi

if [[ -f "$TEXT_LLM_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$TEXT_LLM_ENV_FILE"
  set +a
fi

OMNIVOICE_HOST="${OMNIVOICE_HOST:-${TTS_SERVER_HOST:-127.0.0.1}}"
OMNIVOICE_PORT="${OMNIVOICE_PORT:-${TTS_SERVER_PORT:-8881}}"
OMNIVOICE_API_BASE="${OMNIVOICE_API_BASE:-http://${OMNIVOICE_HOST}:${OMNIVOICE_PORT}}"
OMNIVOICE_MODEL_NAME="${OMNIVOICE_MODEL_NAME:-tts-1}"
OMNIVOICE_SAMPLE_RATE="${OMNIVOICE_SAMPLE_RATE:-24000}"
OMNIVOICE_SERVER_LOG="${OMNIVOICE_SERVER_LOG:-/tmp/omnivoice_announcer_turret_test.log}"

MODEL_API_BASE="${MODEL_API_BASE:-http://127.0.0.1:12434}"
MODEL_NAME="${MODEL_NAME:-hf.co/unsloth/Qwen3-1.7B-GGUF:Q4_K_M}"
MODEL_NAME="${MODEL_NAME%/no_think}"
MODEL_TEMPERATURE="${MODEL_TEMPERATURE:-0.7}"
MODEL_MAX_TOKENS="${MODEL_MAX_TOKENS:-220}"
MODEL_TIMEOUT="${MODEL_TIMEOUT:-45}"

ANNOUNCER_VOICE_NAME="${ANNOUNCER_VOICE_NAME:-clone:announcer_e0}"
TURRET_VOICE_NAME="${TURRET_VOICE_NAME:-clone:turret_e0}"

SERVER_PID=""
REQUEST_PIDS=()

ensure_test_prereqs() {
  if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Missing local venv python at $VENV_PYTHON" >&2
    exit 1
  fi

  if [[ ! -x "$START_SCRIPT" ]]; then
    echo "Missing OmniVoice start script at $START_SCRIPT" >&2
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
}

cleanup_test_state() {
  local pid
  for pid in "${REQUEST_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done

  if [[ -n "$SERVER_PID" ]] && kill -0 "$SERVER_PID" >/dev/null 2>&1; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}

ensure_omnivoice_server() {
  if curl -fsS "$OMNIVOICE_API_BASE/health" >/dev/null 2>&1; then
    return
  fi

  echo "Starting OmniVoice server..."
  "$START_SCRIPT" >"$OMNIVOICE_SERVER_LOG" 2>&1 &
  SERVER_PID="$!"

  for _ in $(seq 1 300); do
    if curl -fsS "$OMNIVOICE_API_BASE/health" >/dev/null 2>&1; then
      return
    fi
    if ! kill -0 "$SERVER_PID" >/dev/null 2>&1; then
      echo "OmniVoice server exited before becoming healthy. Recent log:" >&2
      tail -n 120 "$OMNIVOICE_SERVER_LOG" >&2 || true
      exit 1
    fi
    sleep 1
  done

  echo "Timed out waiting for OmniVoice server at $OMNIVOICE_API_BASE" >&2
  tail -n 120 "$OMNIVOICE_SERVER_LOG" >&2 || true
  exit 1
}

build_tts_request_json() {
  local voice_name="$1"
  local text="$2"

  VOICE_NAME="$voice_name" \
  TEXT="$text" \
  MODEL_NAME="$OMNIVOICE_MODEL_NAME" \
  "$VENV_PYTHON" - <<'PY'
import json
import os

print(json.dumps({
    "model": os.environ["MODEL_NAME"],
    "voice": os.environ["VOICE_NAME"],
    "input": os.environ["TEXT"],
    "stream": True,
    "response_format": "pcm",
}))
PY
}

stream_voice() {
  local voice_name="$1"
  local text="$2"
  local request_json

  request_json="$(build_tts_request_json "$voice_name" "$text")"

  echo
  echo "Prompting $voice_name"
  echo "Text: $text"

  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$request_json" \
    "$OMNIVOICE_API_BASE/v1/audio/speech" \
    | play -q -t raw -b 16 -e signed-integer -c 1 -r "$OMNIVOICE_SAMPLE_RATE" -
}

stream_voice_async() {
  local voice_name="$1"
  local text="$2"
  local request_json

  request_json="$(build_tts_request_json "$voice_name" "$text")"

  echo "Dispatching $voice_name"
  curl -fsS \
    -H "Content-Type: application/json" \
    -d "$request_json" \
    "$OMNIVOICE_API_BASE/v1/audio/speech" \
    | play -q -t raw -b 16 -e signed-integer -c 1 -r "$OMNIVOICE_SAMPLE_RATE" - &

  REQUEST_PIDS+=("$!")
}

wait_for_requests() {
  local pid
  for pid in "${REQUEST_PIDS[@]:-}"; do
    wait "$pid"
  done
  REQUEST_PIDS=()
}

generate_commentary_lines() {
  local scenario_text="$1"
  local line_count="$2"
  local system_prompt="$3"
  local user_header="$4"

  MODEL_API_BASE="$MODEL_API_BASE" \
  MODEL_NAME="$MODEL_NAME" \
  MODEL_TEMPERATURE="$MODEL_TEMPERATURE" \
  MODEL_MAX_TOKENS="$MODEL_MAX_TOKENS" \
  MODEL_TIMEOUT="$MODEL_TIMEOUT" \
  LINE_COUNT="$line_count" \
  SCENARIO_TEXT="$scenario_text" \
  SYSTEM_PROMPT="$system_prompt" \
  USER_HEADER="$user_header" \
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
    content = re.sub(r"<think>.*?</think>\s*", "", content.strip(), flags=re.DOTALL).strip()
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
    return json.loads(raw_text[start:end + 1])


model_api_base = os.environ["MODEL_API_BASE"].rstrip("/")
model_name = os.environ["MODEL_NAME"]
temperature = float(os.environ["MODEL_TEMPERATURE"])
max_tokens = int(os.environ["MODEL_MAX_TOKENS"])
timeout_seconds = float(os.environ["MODEL_TIMEOUT"])
line_count = max(1, int(os.environ["LINE_COUNT"]))
scenario_text = os.environ["SCENARIO_TEXT"]
system_prompt = os.environ["SYSTEM_PROMPT"]
user_header = os.environ["USER_HEADER"]

user_prompt = (
    f"{user_header}\n\n"
    f"Generate {line_count} short lines.\n"
    "Return JSON only with one top-level key named lines.\n"
    "Each line must be immediately speakable.\n"
    "No numbering. No markdown. No labels.\n\n"
    f"Scenario:\n{scenario_text}\n\n"
    'Return JSON like {"lines": ["...", "..."]}'
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

cleaned = []
for value in lines:
    if not isinstance(value, str):
        continue
    line = " ".join(value.strip().split())
    if line:
        cleaned.append(line)
    if len(cleaned) >= line_count:
        break

if not cleaned:
    raise RuntimeError("text model returned no usable lines")

for line in cleaned:
    print(line)
PY
}
