#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

trap cleanup_test_state EXIT

ensure_test_prereqs
ensure_omnivoice_server

VOICE_NAME="${OMNIVOICE_TEST_VOICE_NAME:-$ANNOUNCER_VOICE_NAME}"
TEXT="${TEXT:-This is a live cloned voice playback test.}"

stream_voice "$VOICE_NAME" "$TEXT"
