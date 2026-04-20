#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

trap cleanup_test_state EXIT

ensure_test_prereqs
ensure_omnivoice_server

LINE_COUNT="${LINE_COUNT:-4}"
VOICE_NAME="${OMNIVOICE_TEST_VOICE_NAME:-$ANNOUNCER_VOICE_NAME}"
SCENARIO_TEXT="${SCENARIO_TEXT:-On Mirage in a packed arena, the star rifler cracks open A with two instant headshots, the lurker catches the rotate through connector, and the last defender is denied on the smoke defuse as the crowd erupts.}"

SYSTEM_PROMPT="You are an elite Counter-Strike 2 play-by-play caster. Return short spoken lines for live commentary."
USER_HEADER="Generate punchy live play-by-play lines for one exciting highlight sequence."

mapfile -t TEXTS < <(generate_commentary_lines "$SCENARIO_TEXT" "$LINE_COUNT" "$SYSTEM_PROMPT" "$USER_HEADER")

echo "Generated commentary lines:"
for i in "${!TEXTS[@]}"; do
  printf '  %s. %s\n' "$((i + 1))" "${TEXTS[$i]}"
done

for text in "${TEXTS[@]}"; do
  stream_voice "$VOICE_NAME" "$text"
done
