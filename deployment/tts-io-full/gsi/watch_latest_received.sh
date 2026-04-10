#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_FILE="$SCRIPT_DIR/pipeline/.state/v2/gsi_received_latest.json"
POLL_INTERVAL="${GSI_WATCH_INTERVAL_SECONDS:-0.5}"
LAST_CONTENT=""
LAST_MTIME=""
LAST_SIZE=""

read_file_state() {
  if [ ! -e "$TARGET_FILE" ]; then
    echo "missing|0|"
    return
  fi

  python3 - "$TARGET_FILE" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
stat = path.stat()
print(f"{int(stat.st_mtime_ns)}|{stat.st_size}|")
PY
}

render() {
  printf '\033[2J\033[H'
  echo "Watching latest received GSI JSON"
  echo "File: $TARGET_FILE"
  echo "Updated: $(date '+%Y-%m-%d %H:%M:%S')"
  echo

  if [ -s "$TARGET_FILE" ]; then
    cat "$TARGET_FILE"
  else
    echo "(file is empty)"
  fi
}

while true; do
  FILE_STATE="$(read_file_state)"
  CURRENT_MTIME="${FILE_STATE%%|*}"
  REST="${FILE_STATE#*|}"
  CURRENT_SIZE="${REST%%|*}"
  CURRENT_CONTENT="$(cat "$TARGET_FILE" 2>/dev/null || true)"
  if [ "$CURRENT_CONTENT" != "$LAST_CONTENT" ] || [ "$CURRENT_MTIME" != "$LAST_MTIME" ] || [ "$CURRENT_SIZE" != "$LAST_SIZE" ]; then
    LAST_CONTENT="$CURRENT_CONTENT"
    LAST_MTIME="$CURRENT_MTIME"
    LAST_SIZE="$CURRENT_SIZE"
    render
  fi
  sleep "$POLL_INTERVAL"
done
