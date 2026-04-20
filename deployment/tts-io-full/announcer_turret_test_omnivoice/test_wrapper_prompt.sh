#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/common.sh"

DEFAULT_INPUT_PATH="$ROOT_DIR/../../compacted/target_inputs"
INPUT_PATH="${1:-${JSON_INPUT_PATH:-$DEFAULT_INPUT_PATH}}"
INPUT_INDEX="${2:-${JSON_INPUT_INDEX:-random}}"

if [[ "${INPUT_PATH:-}" == "--help" || "${INPUT_PATH:-}" == "-h" ]]; then
  cat <<EOF
Usage:
  $(basename "$0") [json_or_jsonl_path] [record_index]

Examples:
  $(basename "$0")
  $(basename "$0") /path/to/target_inputs
  $(basename "$0") /path/to/wrapper.json
  $(basename "$0") /path/to/training_wrapper_pretty.jsonl 12

The file may contain:
  - one JSON object
  - multiple JSON objects separated by whitespace
  - a wrapper row with {"input": ...}
  - a bare input object

If the path is a directory, the script scans all .json and .jsonl files under it.
With no arguments, it randomly picks a record from compacted/target_inputs.
EOF
  exit 0
fi

ensure_test_prereqs
ensure_omnivoice_server

if [[ ! -e "$INPUT_PATH" ]]; then
  echo "Input path not found: $INPUT_PATH" >&2
  exit 1
fi

if [[ "$INPUT_INDEX" != "random" ]] && ! [[ "$INPUT_INDEX" =~ ^[0-9]+$ ]]; then
  echo "Record index must be a non-negative integer or 'random': $INPUT_INDEX" >&2
  exit 1
fi

PLAN_FILE="$(mktemp)"
trap 'rm -f "$PLAN_FILE"; cleanup_test_state' EXIT

"$VENV_PYTHON" - "$ROOT_DIR" "$INPUT_PATH" "$INPUT_INDEX" "$PLAN_FILE" <<'PY'
import json
import random
import sys
from pathlib import Path

repo_root = Path(sys.argv[1]).resolve().parent.parent
input_path = Path(sys.argv[2]).resolve()
record_index_arg = sys.argv[3]
plan_path = Path(sys.argv[4]).resolve()

pipeline_dir = repo_root / "deployment" / "tts-io-full" / "gsi" / "pipeline"
sys.path.insert(0, str(pipeline_dir))

import prompt_queue_v4 as pq4  # noqa: E402
from text_llm_client import build_config as build_text_llm_config  # noqa: E402
from text_llm_client import request_chat_completion  # noqa: E402


def parse_json_objects(text):
    decoder = json.JSONDecoder()
    objects = []
    index = 0
    length = len(text)

    while index < length:
        while index < length and text[index].isspace():
            index += 1
        if index >= length:
            break
        obj, next_index = decoder.raw_decode(text, index)
        objects.append(obj)
        index = next_index

    return objects


def normalize_wrapper(obj):
    if isinstance(obj, dict) and isinstance(obj.get("input"), dict):
        return obj
    if isinstance(obj, dict):
        return {"input": obj}
    raise RuntimeError("selected record is not a JSON object")


def build_prompts(wrapper):
    wrapper_input = pq4.as_dict(pq4.as_dict(wrapper).get("input"))
    request = pq4.as_dict(wrapper_input.get("request"))
    mode = str(request.get("mode") or "").strip()

    if mode == "event_bundle":
        current_events = wrapper_input.get("current_events", [])
        return mode, request, pq4.build_event_system_prompt(current_events), pq4.build_event_user_prompt(wrapper), 4

    if mode == "idle_color":
        return mode, request, pq4.build_interval_system_prompt(False), pq4.build_interval_user_prompt(wrapper, False), 3

    if mode == "idle_conversation":
        return mode, request, pq4.build_interval_system_prompt(True), pq4.build_interval_user_prompt(wrapper, True), 3

    raise RuntimeError(f"unsupported request mode: {mode or '<missing>'}")


def collect_input_files(path):
    if path.is_file():
        return [path]

    files = [
        candidate
        for candidate in sorted(path.rglob("*"))
        if candidate.is_file() and candidate.suffix.lower() in {".json", ".jsonl"}
    ]
    if not files:
        raise RuntimeError(f"no .json or .jsonl files found under {path}")
    return files


def build_playback_items(request_lines, raw_lines):
    request_lines = [pq4.as_dict(line) for line in (request_lines or [])]
    if not request_lines:
        raise RuntimeError("request.lines is missing or empty")

    grouped = [[] for _ in request_lines]
    for index, raw_line in enumerate(raw_lines):
        target_index = index if index < len(request_lines) else len(request_lines) - 1
        grouped[target_index].append(raw_line)

    playback = []
    for request_line, texts in zip(request_lines, grouped):
        caster = pq4.normalize_caster_id(request_line.get("caster"))
        prompt_style = request_line.get("style") or ""
        for text in texts:
            for sentence in pq4.split_compound_event_lines([text]):
                playback.append(
                    {
                        "caster": caster,
                        "style": prompt_style,
                        "sentence": sentence,
                    }
                )

    if not playback:
        raise RuntimeError("text model produced no playable sentences")

    return playback


source_path = input_path
if input_path.is_dir():
    candidate_files = collect_input_files(input_path)
    source_path = random.choice(candidate_files)

objects = parse_json_objects(source_path.read_text(encoding="utf-8"))
if not objects:
    raise RuntimeError(f"no JSON objects found in {source_path}")

if record_index_arg == "random":
    record_index = random.randrange(len(objects))
else:
    record_index = int(record_index_arg)
    if record_index >= len(objects):
        raise RuntimeError(
            f"record index {record_index} is out of range for {source_path} ({len(objects)} object(s))"
        )

selected = objects[record_index]
wrapper = normalize_wrapper(selected)
mode, request, system_prompt, user_prompt, expected_max = build_prompts(wrapper)

text_config = build_text_llm_config(repo_root)
result = request_chat_completion(text_config, system_prompt, user_prompt)
raw_lines = pq4.extract_commentary_lines(result["raw_text"], expected_max=expected_max)
playback_items = build_playback_items(request.get("lines"), raw_lines)

plan = {
    "source_path": str(source_path),
    "source_index": record_index,
    "mode": mode,
    "request": request,
    "raw_text": result["raw_text"],
    "raw_lines": raw_lines,
    "playback_items": playback_items,
}

plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY

echo "Source:"
echo "  file:  $("${VENV_PYTHON}" - "$PLAN_FILE" <<'PY'
import json
import sys
plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(plan["source_path"])
PY
)"
echo "  index: $("${VENV_PYTHON}" - "$PLAN_FILE" <<'PY'
import json
import sys
plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
print(plan["source_index"])
PY
)"
echo

"$VENV_PYTHON" - "$PLAN_FILE" <<'PY'
import json
import sys

plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
print("Request:")
print(json.dumps(plan["request"], indent=2, sort_keys=True))
print()
print("Generated lines:")
for index, line in enumerate(plan["raw_lines"], start=1):
    print(f"  {index}. {line}")
print()
print("Playback sentences:")
for index, item in enumerate(plan["playback_items"], start=1):
    print(f"  {index}. [{item['caster']}] {item['sentence']}")
PY

while IFS=$'\t' read -r caster sentence; do
  stream_voice "$(voice_name_for_caster "$caster")" "$sentence"
done < <(
  "$VENV_PYTHON" - "$PLAN_FILE" <<'PY'
import json
import sys

plan = json.loads(open(sys.argv[1], encoding="utf-8").read())
for item in plan["playback_items"]:
    sentence = " ".join(str(item["sentence"]).split())
    if sentence:
        print(f"{item['caster']}\t{sentence}")
PY
)
