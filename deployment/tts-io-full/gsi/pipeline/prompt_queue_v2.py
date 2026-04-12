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
PROMPT_CONFIG_PATH = SCRIPT_DIR / "prompt_config_v2.json"
PROMPT_RUNTIME_HISTORY_PATH = STATE_DIR / "prompt_runtime_pretty.jsonl"
PROMPT_RUNTIME_LATEST_PATH = STATE_DIR / "prompt_runtime_latest.json"
LEGACY_PROMPT_QUEUE_HISTORY_PATH = STATE_DIR / "prompt_queue_pretty.jsonl"
LEGACY_PROMPT_QUEUE_LATEST_PATH = STATE_DIR / "prompt_queue_latest.json"
LEGACY_PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"

TTS_ORDER_LOCK = threading.Lock()
TTS_ORDER_CONDITION = threading.Condition(TTS_ORDER_LOCK)
TTS_PENDING_PLAYBACKS = {}
TTS_NEXT_SUBMISSION_ID = 1
TTS_NEXT_PLAYBACK_ID = 1
TTS_WORKER_THREAD = None


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
    global TTS_PENDING_PLAYBACKS, TTS_NEXT_SUBMISSION_ID, TTS_NEXT_PLAYBACK_ID
    ensure_state_dir()
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
        LEGACY_PROMPT_QUEUE_HISTORY_PATH,
        LEGACY_PROMPT_QUEUE_LATEST_PATH,
        LEGACY_PROMPT_QUEUE_STATE_PATH,
    ]:
        path.write_text("", encoding="utf-8")
    with TTS_ORDER_CONDITION:
        TTS_PENDING_PLAYBACKS = {}
        TTS_NEXT_SUBMISSION_ID = 1
        TTS_NEXT_PLAYBACK_ID = 1


def load_prompt_config():
    if not PROMPT_CONFIG_PATH.exists():
        return {}

    try:
        config = json.loads(PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        return {}

    if not isinstance(config, dict):
        return {}

    return config


def build_instruction():
    instruction = str(load_prompt_config().get("instruction", "")).strip()
    return instruction


def build_gameplay_snapshot_prompt(filtered_batch):
    config = load_prompt_config()
    label = str(config.get("gameplay_snapshot_label", "Gameplay snapshot")).strip() or "Gameplay snapshot"
    return f"{label}:\n" + json.dumps(filtered_batch, indent=2, sort_keys=True), filtered_batch


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


def reserve_tts_submission_id():
    global TTS_NEXT_SUBMISSION_ID
    with TTS_ORDER_CONDITION:
        submission_id = TTS_NEXT_SUBMISSION_ID
        TTS_NEXT_SUBMISSION_ID += 1
        return submission_id


def ensure_tts_worker():
    global TTS_WORKER_THREAD
    with TTS_ORDER_CONDITION:
        if TTS_WORKER_THREAD is not None and TTS_WORKER_THREAD.is_alive():
            return

        def worker():
            global TTS_NEXT_PLAYBACK_ID
            while True:
                with TTS_ORDER_CONDITION:
                    while TTS_NEXT_PLAYBACK_ID not in TTS_PENDING_PLAYBACKS:
                        TTS_ORDER_CONDITION.wait()
                    entry = TTS_PENDING_PLAYBACKS.pop(TTS_NEXT_PLAYBACK_ID)
                    TTS_NEXT_PLAYBACK_ID += 1

                done_event = entry.get("done_event")
                if entry.get("skip"):
                    if done_event is not None:
                        done_event.set()
                    continue

                try:
                    entry["result"] = stream_tts_sequence_playback(
                        entry["tts_config"],
                        [entry["tts_prompt"]],
                    )
                except Exception as error:
                    entry["error"] = str(error)
                finally:
                    done_event.set()

        TTS_WORKER_THREAD = threading.Thread(target=worker, daemon=True)
        TTS_WORKER_THREAD.start()


def skip_tts_submission(submission_id):
    ensure_tts_worker()
    with TTS_ORDER_CONDITION:
        TTS_PENDING_PLAYBACKS[submission_id] = {"skip": True}
        TTS_ORDER_CONDITION.notify_all()


def enqueue_tts_playback_and_wait(submission_id, tts_config, tts_prompt):
    ensure_tts_worker()
    done_event = threading.Event()
    entry = {
        "done_event": done_event,
        "error": None,
        "result": None,
        "tts_config": tts_config,
        "tts_prompt": tts_prompt,
    }
    with TTS_ORDER_CONDITION:
        TTS_PENDING_PLAYBACKS[submission_id] = entry
        TTS_ORDER_CONDITION.notify_all()

    done_event.wait()
    if entry["error"]:
        raise RuntimeError(entry["error"])
    return entry["result"]


def process_filtered_batch(filtered_batch, repo_root, payload_sequence=None):
    if not filtered_batch.get("events"):
        return None

    instruction = build_instruction()
    gameplay_snapshot_prompt, prompt_snapshot = build_gameplay_snapshot_prompt(filtered_batch)
    text_config = build_text_llm_config(repo_root)
    tts_config = build_tts_config(repo_root)
    tts_submission_id = reserve_tts_submission_id()

    record = {
        "created_at": now_stamp(),
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_schema": {
            "instruction": instruction,
            "gameplay_snapshot": prompt_snapshot,
        },
        "llm": {
            "mode": "single_sentence_immediate",
        },
        "tts": {
            "submission_id": tts_submission_id,
            "caster": PROMPT_TTS_CASTER,
            "emotion": PROMPT_TTS_EMOTION,
            "speed": PROMPT_TTS_SPEED,
            "voice_name": tts_config.voice_name,
        },
    }

    llm_result = None
    commentary_text = None
    tts_enqueued = False
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
        tts_enqueued = True
        playback_result = enqueue_tts_playback_and_wait(
            tts_submission_id,
            tts_config,
            tts_prompt,
        )

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
        if not tts_enqueued:
            skip_tts_submission(tts_submission_id)

    finalized_record = strip_empty(record)
    append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized_record)
    write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized_record)
    return finalized_record
