#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

trap cleanup_test_state EXIT

ensure_test_prereqs
ensure_omnivoice_server

ANNOUNCER_TEXT="${ANNOUNCER_TEXT:-This is the announcer voice.}"
TURRET_TEXT="${TURRET_TEXT:-This is the turret voice.}"

stream_voice "$ANNOUNCER_VOICE_NAME" "$ANNOUNCER_TEXT"
stream_voice "$TURRET_VOICE_NAME" "$TURRET_TEXT"

echo
echo "Sequence playback finished."
