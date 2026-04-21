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

This is a timed clone of test_wrapper_prompt.sh.
It prints:
  - text generation completion latency
  - TTS first-PCM latency per sentence
  - TTS total request time per sentence
  - summary averages

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
TTS_TIMINGS_FILE="$(mktemp)"
trap 'rm -f "$PLAN_FILE" "$TTS_TIMINGS_FILE"; cleanup_test_state' EXIT

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

import prompt_queue_v5 as pq5  # noqa: E402


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
    wrapper_input = pq5.as_dict(pq5.as_dict(wrapper).get("input"))
    request = pq5.as_dict(wrapper_input.get("request"))
    mode = str(request.get("mode") or "").strip()

    if mode == "event_bundle":
        current_events = wrapper_input.get("current_events", [])
        return mode, request, pq5.build_event_system_prompt(
            current_events,
            followup_caster=pq5.event_followup_caster_from_wrapper(wrapper),
        ), pq5.build_event_user_prompt(wrapper), 4

    if mode == "idle_color":
        return mode, request, pq5.build_interval_system_prompt(False), pq5.build_interval_user_prompt(wrapper, False), 3

    if mode == "idle_conversation":
        return mode, request, pq5.build_interval_system_prompt(True), pq5.build_interval_user_prompt(wrapper, True), 3

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
    request_lines = [pq5.as_dict(line) for line in (request_lines or [])]
    if not request_lines:
        raise RuntimeError("request.lines is missing or empty")

    grouped = [[] for _ in request_lines]
    for index, raw_line in enumerate(raw_lines):
        target_index = index if index < len(request_lines) else len(request_lines) - 1
        grouped[target_index].append(raw_line)

    playback = []
    for request_line, texts in zip(request_lines, grouped):
        caster = pq5.normalize_caster_id(request_line.get("caster"))
        prompt_style = request_line.get("style") or ""
        for text in texts:
            for sentence in pq5.split_compound_event_lines([text]):
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

text_config = pq5.build_v5_text_llm_config(repo_root)
result, raw_lines = pq5.request_commentary_lines_with_retry(
    text_config,
    system_prompt,
    user_prompt,
    expected_max=expected_max,
)
playback_items = build_playback_items(request.get("lines"), raw_lines)

plan = {
    "source_path": str(source_path),
    "source_index": record_index,
    "mode": mode,
    "request": request,
    "raw_text": result["raw_text"],
    "raw_lines": raw_lines,
    "playback_items": playback_items,
    "text_generation_completion_latency_seconds": result.get("text_generation_completion_latency_seconds"),
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
print()
latency = plan.get("text_generation_completion_latency_seconds")
if isinstance(latency, (int, float)):
    print(f"Text generation completion: {latency:.3f}s")
PY

while IFS=$'\t' read -r caster sentence; do
  TIMING_JSON="$("$VENV_PYTHON" - "$OMNIVOICE_API_BASE" "$OMNIVOICE_MODEL_NAME" "$(voice_name_for_caster "$caster")" "$sentence" "$OMNIVOICE_SAMPLE_RATE" <<'PY'
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

api_base = sys.argv[1]
model_name = sys.argv[2]
voice_name = sys.argv[3]
text = sys.argv[4]
sample_rate = int(sys.argv[5])

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
tmp_file = tempfile.NamedTemporaryFile(prefix="wrapper_prompt_timed_", suffix=".pcm", delete=False)
tmp_path = Path(tmp_file.name)

try:
    with urllib.request.urlopen(request, timeout=120) as response:
        while True:
            chunk = response.read(4096)
            if not chunk:
                break
            if first_chunk_at is None:
                first_chunk_at = time.monotonic()
            tmp_file.write(chunk)
            total_bytes += len(chunk)
finally:
    tmp_file.close()

completed_at = time.monotonic()

result = {
    "pcm_path": str(tmp_path),
    "sample_rate": sample_rate,
    "first_pcm_latency_seconds": None if first_chunk_at is None else first_chunk_at - started_at,
    "tts_total_completion_seconds": completed_at - started_at,
    "total_bytes": total_bytes,
}
print(json.dumps(result))
PY
)"

  PCM_PATH="$("$VENV_PYTHON" - "$TIMING_JSON" <<'PY'
import json
import sys
print(json.loads(sys.argv[1])["pcm_path"])
PY
)"

  echo
  echo "Prompting $(voice_name_for_caster "$caster")"
  echo "Text: $sentence"
  "$VENV_PYTHON" - "$TIMING_JSON" <<'PY'
import json
import sys
timing = json.loads(sys.argv[1])
first_pcm = timing.get("first_pcm_latency_seconds")
total = timing.get("tts_total_completion_seconds")
if isinstance(first_pcm, (int, float)):
    print(f"TTS first PCM: {first_pcm:.3f}s")
if isinstance(total, (int, float)):
    print(f"TTS total completion: {total:.3f}s")
print(f"PCM bytes: {timing.get('total_bytes', 0)}")
PY

  printf '%s\n' "$TIMING_JSON" >> "$TTS_TIMINGS_FILE"
  play -q -t raw -b 16 -e signed-integer -c 1 -r "$OMNIVOICE_SAMPLE_RATE" "$PCM_PATH"
  rm -f "$PCM_PATH"
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

"$VENV_PYTHON" - "$PLAN_FILE" "$TTS_TIMINGS_FILE" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
timings_path = Path(sys.argv[2])
timings = []
for line in timings_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line:
        timings.append(json.loads(line))

def avg(values):
    values = [value for value in values if isinstance(value, (int, float))]
    if not values:
        return None
    return sum(values) / len(values)

print()
print("Summary:")
text_latency = plan.get("text_generation_completion_latency_seconds")
if isinstance(text_latency, (int, float)):
    print(f"  text completion: {text_latency:.3f}s")

avg_first_pcm = avg([item.get("first_pcm_latency_seconds") for item in timings])
avg_tts_total = avg([item.get("tts_total_completion_seconds") for item in timings])

if isinstance(avg_first_pcm, (int, float)):
    print(f"  avg TTS first PCM: {avg_first_pcm:.3f}s")
if isinstance(avg_tts_total, (int, float)):
    print(f"  avg TTS total completion: {avg_tts_total:.3f}s")
print(f"  sentences played: {len(timings)}")
PY
