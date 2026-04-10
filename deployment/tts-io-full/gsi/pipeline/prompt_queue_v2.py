import json
import os
import re
import threading
from datetime import datetime
from pathlib import Path

from text_llm_client import (
    build_config as build_text_llm_config,
    request_chat_completion,
)
from tts_client import (
    build_config as build_tts_config,
    stream_tts_sequence_playback,
)


def load_local_env():
    env_path = Path(__file__).with_name(".env")
    values = {}

    if not env_path.exists():
        return values

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value

    return values


ENV_FILE_VALUES = load_local_env()
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state" / "v2"
PROMPT_RUNTIME_HISTORY_PATH = STATE_DIR / "prompt_runtime_pretty.jsonl"
PROMPT_RUNTIME_LATEST_PATH = STATE_DIR / "prompt_runtime_latest.json"
LEGACY_PROMPT_QUEUE_HISTORY_PATH = STATE_DIR / "prompt_queue_pretty.jsonl"
LEGACY_PROMPT_QUEUE_LATEST_PATH = STATE_DIR / "prompt_queue_latest.json"
LEGACY_PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"

PROMPT_LOCK = threading.Lock()


def env_text(name, default=""):
    return os.environ.get(name, ENV_FILE_VALUES.get(name, default))


def env_int(name, default):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_float(name, default):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


PROMPT_TTS_CASTER = env_text("PROMPT_TTS_CASTER", "play_by_play")
PROMPT_TTS_EMOTION = env_text("PROMPT_TTS_EMOTION", "excited")
PROMPT_TTS_SPEED = env_float("PROMPT_TTS_SPEED", 1.12)
PROMPT_INSTRUCTION_OVERRIDE = env_text("PROMPT_INSTRUCTION", "").strip()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
    ]:
        path.touch(exist_ok=True)


def append_pretty_json_record(path, record):
    ensure_state_dir()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True))
        handle.write("\n\n")
        handle.flush()


def write_pretty_json_file(path, record):
    ensure_state_dir()
    path.write_text(f"{json.dumps(record, indent=2, sort_keys=True)}\n", encoding="utf-8")


def reset_prompt_runtime_state():
    ensure_state_dir()
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
        LEGACY_PROMPT_QUEUE_HISTORY_PATH,
        LEGACY_PROMPT_QUEUE_LATEST_PATH,
        LEGACY_PROMPT_QUEUE_STATE_PATH,
    ]:
        path.write_text("", encoding="utf-8")


def build_instruction():
    if PROMPT_INSTRUCTION_OVERRIDE:
        return PROMPT_INSTRUCTION_OVERRIDE

    return (
        "You are a Counter-Strike 2 caster. "
        "No thinking. No explanations. "
        "Return only one short sentence as plain text. "
        "Ideally use fewer than 5 words and never exceed 8 words. "
        "Prioritize the main important event and its trigger over secondary gameplay snapshot details. "
        "Use player names only when they are clearly given. "
        "Never mention entity ids, observer slots, raw runtime identifiers, or JSON field names. "
        "Use fast, speakable phrasing. "
        "No JSON. No markdown. No labels. No code fences."
    )
def build_gameplay_snapshot_prompt(filtered_batch):
    return "Gameplay snapshot:\n" + json.dumps(filtered_batch, indent=2, sort_keys=True), filtered_batch


def extract_commentary_text(raw_text):
    cleaned_segments = []

    for block in raw_text.splitlines():
        stripped_block = " ".join(block.strip().split())
        if not stripped_block:
            continue
        split_segments = re.split(r"(?<=[.!?])\s+", stripped_block)
        cleaned_segments.extend(segment for segment in split_segments if segment)

    for value in cleaned_segments:
        line = " ".join(value.strip().split())
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", line)
        if not line:
            continue
        return line

    if not cleaned_segments:
        raise RuntimeError("text model returned no usable commentary text")

    raise RuntimeError("text model returned no usable commentary text")


def build_tts_prompt(commentary_text, tts_config):
    return {
        "commentary": commentary_text,
        "caster": PROMPT_TTS_CASTER,
        "emotion": PROMPT_TTS_EMOTION,
        "speed": PROMPT_TTS_SPEED,
        "voice_name": tts_config.voice_name,
    }


def strip_empty(value):
    if isinstance(value, dict):
        cleaned = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [strip_empty(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    return value


def process_filtered_batch(filtered_batch, repo_root):
    if not filtered_batch.get("events"):
        return None

    instruction = build_instruction()
    gameplay_snapshot_prompt, prompt_snapshot = build_gameplay_snapshot_prompt(filtered_batch)
    text_config = build_text_llm_config(repo_root)
    tts_config = build_tts_config(repo_root)

    record = {
        "created_at": now_stamp(),
        "payload_sequence": filtered_batch.get("payload_sequence"),
        "status": "started",
        "prompt_schema": {
            "instruction": instruction,
            "gameplay_snapshot": prompt_snapshot,
        },
        "llm": {
            "mode": "single_sentence_immediate",
        },
        "tts": {
            "caster": PROMPT_TTS_CASTER,
            "emotion": PROMPT_TTS_EMOTION,
            "speed": PROMPT_TTS_SPEED,
            "voice_name": tts_config.voice_name,
        },
    }

    with PROMPT_LOCK:
        llm_result = None
        commentary_text = None
        try:
            llm_result = request_chat_completion(
                text_config,
                instruction,
                gameplay_snapshot_prompt,
            )
            record["llm"]["request"] = llm_result["request"]
            record["llm"]["raw_text"] = llm_result["raw_text"]
            commentary_text = extract_commentary_text(
                llm_result["raw_text"],
            )
            record["llm"]["commentary_text"] = commentary_text
            tts_prompt = build_tts_prompt(commentary_text, tts_config)
            playback_result = stream_tts_sequence_playback(tts_config, [tts_prompt])

            record["status"] = "completed"
            record["tts"].update(
                {
                    "line_count": 1,
                    "commentary_text": commentary_text,
                    "playback": playback_result,
                }
            )
        except Exception as error:
            record["status"] = "failed"
            record["error"] = str(error)

        finalized_record = strip_empty(record)
        append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized_record)
        write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized_record)
        return finalized_record
