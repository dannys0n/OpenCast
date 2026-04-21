#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

PLAY_PID=""
TEMP_DIR=""
AGGREGATE_FIFO=""
STREAM_FIRST_WRITE_TS=""
STREAM_LAST_WRITE_TS=""

cleanup_sequence_test() {
  local pid

  if [[ -n "$PLAY_PID" ]] && kill -0 "$PLAY_PID" >/dev/null 2>&1; then
    kill "$PLAY_PID" >/dev/null 2>&1 || true
    wait "$PLAY_PID" >/dev/null 2>&1 || true
  fi

  for pid in "${REQUEST_PIDS[@]:-}"; do
    if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
      kill "$pid" >/dev/null 2>&1 || true
      wait "$pid" >/dev/null 2>&1 || true
    fi
  done

  REQUEST_PIDS=()

  if [[ -n "$AGGREGATE_FIFO" && -p "$AGGREGATE_FIFO" ]]; then
    rm -f "$AGGREGATE_FIFO"
  fi

  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi

  cleanup_test_state
}

trap cleanup_sequence_test EXIT

ensure_test_prereqs
ensure_omnivoice_server

if ! command -v mktemp >/dev/null 2>&1; then
  echo "mktemp is required on PATH" >&2
  exit 1
fi

monotonic_now() {
  "$VENV_PYTHON" - <<'PY'
import time
print(f"{time.monotonic():.9f}")
PY
}

seconds_diff() {
  local start_ts="$1"
  local end_ts="$2"
  "$VENV_PYTHON" - "$start_ts" "$end_ts" <<'PY'
import sys
start = float(sys.argv[1])
end = float(sys.argv[2])
print(f"{end - start:.3f}")
PY
}

ANNOUNCER_TEXT="${ANNOUNCER_TEXT:-This is the announcer voice.}"
TURRET_TEXT="${TURRET_TEXT:-This is the turret voice.}"
TMP_ROOT="${TMPDIR:-/tmp}"

TEMP_DIR="$(mktemp -d "$TMP_ROOT/omnivoice_simultaneous_sequence_timed.XXXXXX")"
AGGREGATE_FIFO="$TEMP_DIR/sequence.pcm"
mkfifo "$AGGREGATE_FIFO"

declare -a VOICES=(
  "$ANNOUNCER_VOICE_NAME"
  "$TURRET_VOICE_NAME"
)

declare -a TEXTS=(
  "$ANNOUNCER_TEXT"
  "$TURRET_TEXT"
)

declare -a BUFFER_FILES=()
declare -a DONE_FILES=()
declare -a STATUS_FILES=()
declare -a TIMING_FILES=()

dispatch_request() {
  local index="$1"
  local voice_name="$2"
  local text="$3"
  local buffer_file="$TEMP_DIR/request_${index}.pcm"
  local done_file="$TEMP_DIR/request_${index}.done"
  local status_file="$TEMP_DIR/request_${index}.status"
  local timing_file="$TEMP_DIR/request_${index}.timing.json"

  : >"$buffer_file"
  BUFFER_FILES+=("$buffer_file")
  DONE_FILES+=("$done_file")
  STATUS_FILES+=("$status_file")
  TIMING_FILES+=("$timing_file")

  echo "Dispatching $(normalize_omnivoice_voice_name "$voice_name") for queued playback"
  (
    status=0
    if "$VENV_PYTHON" - "$OMNIVOICE_API_BASE" "$OMNIVOICE_MODEL_NAME" "$voice_name" "$text" "$buffer_file" "$timing_file" <<'PY'
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

api_base = sys.argv[1]
model_name = sys.argv[2]
voice_name = sys.argv[3]
text = sys.argv[4]
buffer_path = Path(sys.argv[5])
timing_path = Path(sys.argv[6])

payload = {
    "model": model_name,
    "voice": voice_name,
    "input": text,
    "stream": True,
    "response_format": "pcm",
}

bool_fields = {
    "OMNIVOICE_TTS_DENOISE": "denoise",
    "OMNIVOICE_TTS_PREPROCESS_PROMPT": "preprocess_prompt",
    "OMNIVOICE_TTS_POSTPROCESS_OUTPUT": "postprocess_output",
}
float_fields = {
    "OMNIVOICE_TTS_GUIDANCE_SCALE": "guidance_scale",
    "OMNIVOICE_TTS_T_SHIFT": "t_shift",
    "OMNIVOICE_TTS_POSITION_TEMPERATURE": "position_temperature",
    "OMNIVOICE_TTS_CLASS_TEMPERATURE": "class_temperature",
    "OMNIVOICE_TTS_DURATION": "duration",
    "OMNIVOICE_TTS_LAYER_PENALTY_FACTOR": "layer_penalty_factor",
    "OMNIVOICE_TTS_AUDIO_CHUNK_DURATION": "audio_chunk_duration",
    "OMNIVOICE_TTS_AUDIO_CHUNK_THRESHOLD": "audio_chunk_threshold",
}
int_fields = {
    "OMNIVOICE_TTS_NUM_STEP": "num_step",
}
str_fields = {
    "OMNIVOICE_TTS_LANGUAGE": "language",
}

for env_name, field_name in bool_fields.items():
    value = os.environ.get(env_name, "").strip().lower()
    if value in {"1", "true", "yes", "on"}:
        payload[field_name] = True
    elif value in {"0", "false", "no", "off"}:
        payload[field_name] = False

for env_name, field_name in float_fields.items():
    value = os.environ.get(env_name, "").strip()
    if value:
        payload[field_name] = float(value)

for env_name, field_name in int_fields.items():
    value = os.environ.get(env_name, "").strip()
    if value:
        payload[field_name] = int(value)

for env_name, field_name in str_fields.items():
    value = os.environ.get(env_name, "").strip()
    if value:
        payload[field_name] = value

request = urllib.request.Request(
    f"{api_base}/v1/audio/speech",
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)

started_at = time.monotonic()
first_chunk_at = None
total_bytes = 0

try:
    with urllib.request.urlopen(request, timeout=120) as response:
        with buffer_path.open("wb") as handle:
            while True:
                chunk = response.read(4096)
                if not chunk:
                    break
                if first_chunk_at is None:
                    first_chunk_at = time.monotonic()
                handle.write(chunk)
                handle.flush()
                total_bytes += len(chunk)
    completed_at = time.monotonic()
    payload = {
        "voice": voice_name,
        "text": text,
        "first_pcm_latency_seconds": None if first_chunk_at is None else first_chunk_at - started_at,
        "tts_total_completion_seconds": completed_at - started_at,
        "total_bytes": total_bytes,
        "status": 0,
    }
    timing_path.write_text(json.dumps(payload), encoding="utf-8")
except Exception as error:
    completed_at = time.monotonic()
    payload = {
        "voice": voice_name,
        "text": text,
        "first_pcm_latency_seconds": None if first_chunk_at is None else first_chunk_at - started_at,
        "tts_total_completion_seconds": completed_at - started_at,
        "total_bytes": total_bytes,
        "status": 1,
        "error": str(error),
    }
    timing_path.write_text(json.dumps(payload), encoding="utf-8")
    raise
PY
    then
      status=0
    else
      status=$?
    fi
    printf '%s\n' "$status" >"$status_file"
    touch "$done_file"
    exit "$status"
  ) &
  REQUEST_PIDS+=("$!")
}

stream_buffer_into_fd() {
  local buffer_file="$1"
  local done_file="$2"
  local status_file="$3"
  local offset=0
  local size=0
  local to_copy=0
  local status=0
  STREAM_FIRST_WRITE_TS=""
  STREAM_LAST_WRITE_TS=""

  while :; do
    size="$(stat -c '%s' "$buffer_file" 2>/dev/null || printf '0')"
    if (( size > offset )); then
      to_copy=$((size - offset))
      if [[ -z "$STREAM_FIRST_WRITE_TS" ]]; then
        STREAM_FIRST_WRITE_TS="$(monotonic_now)"
      fi
      dd if="$buffer_file" bs=1 skip="$offset" count="$to_copy" status=none >&3
      STREAM_LAST_WRITE_TS="$(monotonic_now)"
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
play -q -t raw -b 16 -e signed-integer -c 1 -r "$OMNIVOICE_SAMPLE_RATE" "$AGGREGATE_FIFO" &
PLAY_PID="$!"

exec 3>"$AGGREGATE_FIFO"

for i in "${!VOICES[@]}"; do
  dispatch_request "$i" "${VOICES[$i]}" "${TEXTS[$i]}"
done

echo
echo "All requests dispatched. Streaming them in order through one playback session..."

previous_stream_end_ts=""

for i in "${!BUFFER_FILES[@]}"; do
  echo "Queueing $(normalize_omnivoice_voice_name "${VOICES[$i]}")"
  stream_buffer_into_fd "${BUFFER_FILES[$i]}" "${DONE_FILES[$i]}" "${STATUS_FILES[$i]}"
  if [[ -f "${TIMING_FILES[$i]}" ]]; then
    if [[ -n "$previous_stream_end_ts" && -n "$STREAM_FIRST_WRITE_TS" ]]; then
      handoff_gap="$(seconds_diff "$previous_stream_end_ts" "$STREAM_FIRST_WRITE_TS")"
    else
      handoff_gap=""
    fi
    "$VENV_PYTHON" - "${TIMING_FILES[$i]}" "${handoff_gap:-}" <<'PY'
import json
import sys

timing = json.loads(open(sys.argv[1], encoding="utf-8").read())
handoff_gap = sys.argv[2].strip()
voice = timing.get("voice", "unknown")
first_pcm = timing.get("first_pcm_latency_seconds")
total = timing.get("tts_total_completion_seconds")
print(f"  {voice}")
if isinstance(first_pcm, (int, float)):
    print(f"    first PCM: {first_pcm:.3f}s")
if isinstance(total, (int, float)):
    print(f"    total completion: {total:.3f}s")
if handoff_gap:
    print(f"    playback handoff gap: {handoff_gap}s")
print(f"    PCM bytes: {timing.get('total_bytes', 0)}")
PY
  fi
  if [[ -n "$STREAM_LAST_WRITE_TS" ]]; then
    previous_stream_end_ts="$STREAM_LAST_WRITE_TS"
  fi
done

exec 3>&-

wait_for_requests
wait "$PLAY_PID"

echo
"$VENV_PYTHON" - "${TIMING_FILES[@]}" <<'PY'
import json
import sys

timings = []
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as handle:
        timings.append(json.load(handle))

def avg(values):
    values = [value for value in values if isinstance(value, (int, float))]
    if not values:
        return None
    return sum(values) / len(values)

avg_first_pcm = avg([item.get("first_pcm_latency_seconds") for item in timings])
avg_total = avg([item.get("tts_total_completion_seconds") for item in timings])

print("Summary:")
if isinstance(avg_first_pcm, (int, float)):
    print(f"  avg TTS first PCM: {avg_first_pcm:.3f}s")
if isinstance(avg_total, (int, float)):
    print(f"  avg TTS total completion: {avg_total:.3f}s")
print(f"  requests: {len(timings)}")
PY

echo
echo "Simultaneous queued playback finished."
