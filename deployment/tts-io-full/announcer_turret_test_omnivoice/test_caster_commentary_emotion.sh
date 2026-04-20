#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

trap cleanup_test_state EXIT

ensure_test_prereqs
ensure_omnivoice_server

LINE_COUNT="${LINE_COUNT:-3}"
SCENARIO_TEXT="${SCENARIO_TEXT:-On Mirage in a packed arena, the star rifler cracks open A with two instant headshots, the lurker catches the rotate through connector, and the last defender is denied on the smoke defuse as the crowd erupts.}"
VOICE_NAMES=("$ANNOUNCER_VOICE_NAME" "$TURRET_VOICE_NAME" "$ANNOUNCER_VOICE_NAME")

SYSTEM_PROMPT="You are writing a tiny two-caster Counter-Strike exchange. Caster one is dry and clinical. Caster two is polite and reactive. Keep every line short and easy to speak."
USER_HEADER="Generate a brief three-line exchange about one exciting CS2 highlight. Let the second line react more emotionally, then let the third line snap back into concise analysis."

mapfile -t TEXTS < <(generate_commentary_lines "$SCENARIO_TEXT" "$LINE_COUNT" "$SYSTEM_PROMPT" "$USER_HEADER")

echo "Generated commentary lines:"
for i in "${!TEXTS[@]}"; do
  printf '  %s. %s\n' "$((i + 1))" "${TEXTS[$i]}"
done

for i in "${!TEXTS[@]}"; do
  voice_name="${VOICE_NAMES[$i]:-$ANNOUNCER_VOICE_NAME}"
  stream_voice "$voice_name" "${TEXTS[$i]}"
done
