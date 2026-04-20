#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

PLAY_PID=""
TEMP_DIR=""
AGGREGATE_FIFO=""

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

ANNOUNCER_TEXT="${ANNOUNCER_TEXT:-This is the announcer voice.}"
TURRET_TEXT="${TURRET_TEXT:-This is the turret voice.}"
TMP_ROOT="${TMPDIR:-/tmp}"

TEMP_DIR="$(mktemp -d "$TMP_ROOT/omnivoice_simultaneous_sequence.XXXXXX")"
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

  echo "Dispatching $(normalize_omnivoice_voice_name "$voice_name") for queued playback"
  (
    status=0
    if curl -fsS \
      -H "Content-Type: application/json" \
      -d "$request_json" \
      "$OMNIVOICE_API_BASE/v1/audio/speech" \
      >"$buffer_file"; then
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
play -q -t raw -b 16 -e signed-integer -c 1 -r "$OMNIVOICE_SAMPLE_RATE" "$AGGREGATE_FIFO" &
PLAY_PID="$!"

exec 3>"$AGGREGATE_FIFO"

for i in "${!VOICES[@]}"; do
  dispatch_request "$i" "${VOICES[$i]}" "${TEXTS[$i]}"
done

echo
echo "All requests dispatched. Streaming them in order through one playback session..."

for i in "${!BUFFER_FILES[@]}"; do
  echo "Queueing $(normalize_omnivoice_voice_name "${VOICES[$i]}")"
  stream_buffer_into_fd "${BUFFER_FILES[$i]}" "${DONE_FILES[$i]}" "${STATUS_FILES[$i]}"
done

exec 3>&-

wait_for_requests
wait "$PLAY_PID"

echo
echo "Simultaneous queued playback finished."
