#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_FILE="$SCRIPT_DIR/pipeline/.state/v2/gsi_filtered_latest.json"
POLL_INTERVAL="${GSI_WATCH_INTERVAL_SECONDS:-0.5}"
LAST_CONTENT=""

render() {
  printf '\033[2J\033[H'
  echo "Watching latest filtered GSI JSON"
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
  CURRENT_CONTENT="$(cat "$TARGET_FILE" 2>/dev/null || true)"
  if [ "$CURRENT_CONTENT" != "$LAST_CONTENT" ]; then
    LAST_CONTENT="$CURRENT_CONTENT"
    render
  fi
  sleep "$POLL_INTERVAL"
done
