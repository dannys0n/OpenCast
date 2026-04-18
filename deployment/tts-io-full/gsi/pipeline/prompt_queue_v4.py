import copy
import json
import os
import random
import re
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from gsi_prompt_pipeline_v2 import as_dict
from text_llm_client import build_config as build_text_llm_config
from text_llm_client import request_chat_completion
from tts_client import (
    build_config as build_tts_config,
    stream_tts_playback_interruptibly as stream_tts_playback_interruptibly_direct,
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
STATE_DIR = SCRIPT_DIR / ".state" / "v4"
PROMPT_RUNTIME_HISTORY_PATH = STATE_DIR / "prompt_runtime_pretty.jsonl"
PROMPT_RUNTIME_LATEST_PATH = STATE_DIR / "prompt_runtime_latest.json"
PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"
PROMPT_CONFIG_PATH = SCRIPT_DIR / "prompt_config_v4.json"
CHEMISTRY_LINES_PATH = SCRIPT_DIR / "chemistry_lines_v4.json"

QUEUE_LOCK = threading.Lock()
QUEUE_CONDITION = threading.Condition(QUEUE_LOCK)
PLAYBACK_QUEUE = deque()
CURRENT_BUNDLE = None
CURRENT_ITEM = None
QUEUE_WORKER_THREAD = None
QUEUE_MONITOR_THREAD = None
ITEM_SEQUENCE = 0
BUNDLE_SEQUENCE = 0
IDLE_MODE_INDEX = 0
EVENT_QUEUE_OVERFLOW_STARTED_AT = None
LAST_LOG_MONOTONIC = None
LOG_OUTPUT_LOCK = threading.Lock()
OPEN_TTS_LOG_ITEM_ID = None
OPEN_TTS_LOG_STARTED_AT = None
ANSI_RESET = "\033[0m"
ANSI_DIM = "\033[2m"
ANSI_CYAN = "\033[36m"
ANSI_GREEN = "\033[32m"
ANSI_YELLOW = "\033[33m"
ANSI_RED = "\033[31m"
ANSI_BLUE = "\033[34m"
ANSI_MAGENTA = "\033[35m"
ANSI_BRIGHT_CYAN = "\033[96m"
ANSI_BRIGHT_YELLOW = "\033[93m"
CASTER0 = "caster0"
CASTER1 = "caster1"
EVENT_QUEUE_DEQUEUE_SECONDS = 5.0
LEGACY_CASTER_MAP = {
    "play_by_play": CASTER0,
    "color": CASTER1,
    CASTER0: CASTER0,
    CASTER1: CASTER1,
}


def env_text(name, default=""):
    return os.environ.get(name, ENV_FILE_VALUES.get(name, default))


def env_float(name, default):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


PLAY_BY_PLAY_VOICE_NAME = env_text("V4_PLAY_BY_PLAY_VOICE_NAME", env_text("PLAY_BY_PLAY_VOICE_NAME", ""))
COLOR_VOICE_NAME = env_text("V4_COLOR_VOICE_NAME", env_text("COLOR_VOICE_NAME", ""))
PLAY_BY_PLAY_SPEED = env_float("V4_PLAY_BY_PLAY_SPEED", 1.08)
COLOR_SPEED = env_float("V4_COLOR_SPEED", 1.0)


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def strip_empty(value):
    if isinstance(value, dict):
        cleaned = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [strip_empty(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    return value


def reset_log_clock():
    global LAST_LOG_MONOTONIC, OPEN_TTS_LOG_ITEM_ID, OPEN_TTS_LOG_STARTED_AT
    LAST_LOG_MONOTONIC = None
    OPEN_TTS_LOG_ITEM_ID = None
    OPEN_TTS_LOG_STARTED_AT = None


def slim_commentary(text, limit=140):
    compact = " ".join(str(text or "").split())
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def should_use_color():
    if os.environ.get("NO_COLOR"):
        return False
    return bool(sys.stdout.isatty())


def colorize(text, ansi_code):
    if not should_use_color() or not text:
        return text
    return f"{ansi_code}{text}{ANSI_RESET}"


def action_color(action):
    return {
        "prompt": ANSI_CYAN,
        "tts start": ANSI_GREEN,
        "tts interrupted": ANSI_YELLOW,
        "tts failed": ANSI_RED,
        "queue trim": ANSI_YELLOW,
    }.get(action, "")


def tag_color(tag):
    return {
        "event": ANSI_RED,
        "analysis": ANSI_BLUE,
        "idle": ANSI_MAGENTA,
        "chemistry": ANSI_GREEN,
    }.get(tag, "")


def normalize_caster_id(caster):
    value = str(caster or "").strip().lower()
    return LEGACY_CASTER_MAP.get(value, value)


def caster_color(caster):
    return {
        CASTER0: ANSI_BRIGHT_CYAN,
        CASTER1: ANSI_BRIGHT_YELLOW,
    }.get(normalize_caster_id(caster), "")


def caster_label(caster):
    normalized = normalize_caster_id(caster)
    return normalized or str(caster or "")


def slim_log(action, *, tag=None, caster=None, commentary=None, include_commentary=False):
    with LOG_OUTPUT_LOCK:
        _close_open_tts_log_line_locked()
        print(_build_slim_log_text(action, tag=tag, caster=caster, commentary=commentary, include_commentary=include_commentary), flush=True)


def _build_slim_log_text(action, *, tag=None, caster=None, commentary=None, include_commentary=False, delta_override=None):
    global LAST_LOG_MONOTONIC
    now = time.monotonic()
    if delta_override is None:
        if LAST_LOG_MONOTONIC is None:
            delta = 0.0
        else:
            delta = now - LAST_LOG_MONOTONIC
        LAST_LOG_MONOTONIC = now
    else:
        delta = delta_override
    prefix = colorize(f"[+{delta:0.3f}s] {action}", ANSI_DIM)
    action_text = colorize(action, action_color(action))
    parts = []
    if tag:
        parts.append(colorize(f"[{tag}]", tag_color(tag)))
    if caster:
        parts.append(colorize(f"[{caster_label(caster)}]", caster_color(caster)))
    if include_commentary and commentary:
        parts.append(f"\"{slim_commentary(commentary)}\"")
    suffix = " ".join(parts)
    if suffix:
        return f"{prefix.replace(action, action_text, 1)} -> {suffix}"
    return prefix.replace(action, action_text, 1)


def _close_open_tts_log_line_locked():
    global OPEN_TTS_LOG_ITEM_ID, OPEN_TTS_LOG_STARTED_AT
    if OPEN_TTS_LOG_ITEM_ID is not None:
        print("", flush=True)
        OPEN_TTS_LOG_ITEM_ID = None
        OPEN_TTS_LOG_STARTED_AT = None


def slim_log_tts_start(item_id, *, tag=None, caster=None):
    global OPEN_TTS_LOG_ITEM_ID, OPEN_TTS_LOG_STARTED_AT
    with LOG_OUTPUT_LOCK:
        _close_open_tts_log_line_locked()
        print(_build_slim_log_text("tts start", tag=tag, caster=caster), end="", flush=True)
        OPEN_TTS_LOG_ITEM_ID = item_id
        OPEN_TTS_LOG_STARTED_AT = time.monotonic()


def slim_log_tts_finish(item_id, action, *, commentary=None, include_commentary=False):
    global OPEN_TTS_LOG_ITEM_ID, OPEN_TTS_LOG_STARTED_AT
    with LOG_OUTPUT_LOCK:
        suffix = _build_slim_log_text(
            action,
            commentary=commentary,
            include_commentary=include_commentary,
        )
        if OPEN_TTS_LOG_ITEM_ID == item_id:
            print(f" {suffix}", flush=True)
            OPEN_TTS_LOG_ITEM_ID = None
            OPEN_TTS_LOG_STARTED_AT = None
            return
        print(suffix, flush=True)


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
        PROMPT_QUEUE_STATE_PATH,
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
    global PLAYBACK_QUEUE, CURRENT_BUNDLE, CURRENT_ITEM, QUEUE_WORKER_THREAD, QUEUE_MONITOR_THREAD
    global ITEM_SEQUENCE, BUNDLE_SEQUENCE, IDLE_MODE_INDEX, EVENT_QUEUE_OVERFLOW_STARTED_AT
    ensure_state_dir()
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
        PROMPT_QUEUE_STATE_PATH,
    ]:
        path.write_text("", encoding="utf-8")
    reset_log_clock()
    with QUEUE_CONDITION:
        PLAYBACK_QUEUE = deque()
        CURRENT_BUNDLE = None
        CURRENT_ITEM = None
        QUEUE_WORKER_THREAD = None
        QUEUE_MONITOR_THREAD = None
        ITEM_SEQUENCE = 0
        BUNDLE_SEQUENCE = 0
        IDLE_MODE_INDEX = 0
        EVENT_QUEUE_OVERFLOW_STARTED_AT = None


def load_prompt_config():
    if not PROMPT_CONFIG_PATH.exists():
        return {
            "event_instruction": (
                "You are generating controlled Counter-Strike 2 commentary for live TTS. "
                "Return exactly 2 strings in a JSON array. "
                "Item 0 is the event trigger and item 1 is a short event analysis. "
                "The trigger must be 1 to 5 words."
            ),
            "idle_analysis_instruction": (
                "You are generating controlled Counter-Strike 2 idle analysis for live TTS. "
                "Return exactly 3 strings in a JSON array: analysis, comment, response. "
                "Each line must be one short sentence."
            ),
        }

    try:
        loaded = json.loads(PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def load_chemistry_sets():
    if not CHEMISTRY_LINES_PATH.exists():
        return []

    try:
        loaded = json.loads(CHEMISTRY_LINES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    return loaded if isinstance(loaded, list) else []


def next_item_sequence():
    global ITEM_SEQUENCE
    with QUEUE_CONDITION:
        ITEM_SEQUENCE += 1
        return ITEM_SEQUENCE


def next_bundle_sequence():
    global BUNDLE_SEQUENCE
    with QUEUE_CONDITION:
        BUNDLE_SEQUENCE += 1
        return BUNDLE_SEQUENCE


def next_idle_mode():
    global IDLE_MODE_INDEX
    with QUEUE_CONDITION:
        mode = "idle_analysis" if IDLE_MODE_INDEX % 2 == 0 else "chemistry"
        IDLE_MODE_INDEX += 1
        return mode


def build_event_system_prompt():
    config = load_prompt_config()
    return (
        f"{config.get('event_instruction', '').strip()} "
        'Return exactly one JSON array with 2 strings: ["event trigger", "event analysis"]. '
        "The first string must be the event trigger. "
        "The second string must be the event analysis. "
        "No markdown. No numbering. No extra keys. No extra text."
    ).strip()


def build_idle_analysis_system_prompt():
    config = load_prompt_config()
    return (
        f"{config.get('idle_analysis_instruction', '').strip()} "
        'Return exactly one JSON array with 3 strings: ["analysis", "comment", "response"]. '
        "No markdown. No numbering. No extra keys. No extra text."
    ).strip()


def build_event_user_prompt(wrapper):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "event_descriptions": wrapper_input.get("event_descriptions"),
            "current_events": wrapper_input.get("current_events"),
            "request": wrapper_input.get("request"),
        }
    )
    return (
        "Produce controlled event commentary.\n"
        "Use the event descriptions exactly when they already sound natural.\n"
        "Keep array item 0 punchy and literal.\n"
        "Keep array item 1 short and directly tied to the listed events.\n"
        "Return valid JSON only.\n\n"
        "Input JSON:\n"
        f"{json.dumps(prompt_input, indent=2, sort_keys=True)}"
    )


def build_idle_analysis_user_prompt(wrapper):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "score": wrapper_input.get("score"),
            "player_locations": wrapper_input.get("player_locations"),
            "request": wrapper_input.get("request"),
        }
    )
    return (
        "Produce controlled idle analysis.\n"
        "Base the exchange on the player locations.\n"
        "Keep all three lines short and speakable.\n\n"
        "Return valid JSON only.\n\n"
        "Input JSON:\n"
        f"{json.dumps(prompt_input, indent=2, sort_keys=True)}"
    )


def extract_commentary_lines(raw_text, expected_max):
    lines = []
    for block in raw_text.splitlines():
        line = re.sub(r"^[\-\*\d\.\)\s]+", "", " ".join(block.strip().split()))
        if line:
            lines.append(line)

    if not lines:
        compact = " ".join(raw_text.split())
        if compact:
            lines = re.split(r"(?<=[.!?])\s+", compact)

    cleaned = []
    for line in lines:
        candidate = line.strip().strip("`")
        if not candidate:
            continue
        cleaned.append(candidate)
        if len(cleaned) >= expected_max:
            break

    if not cleaned:
        raise RuntimeError("text model returned no usable commentary lines")

    return cleaned


def parse_json_line_array(raw_text, expected_count):
    text = str(raw_text or "").strip()
    parsed = None

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                parsed = None

    if isinstance(parsed, list):
        cleaned = []
        for item in parsed:
            candidate = " ".join(str(item or "").split()).strip()
            if candidate:
                cleaned.append(candidate)
        if len(cleaned) >= expected_count:
            return cleaned[:expected_count]

    fallback_lines = extract_commentary_lines(raw_text, expected_max=expected_count)
    if len(fallback_lines) >= expected_count:
        return fallback_lines[:expected_count]

    raise RuntimeError(f"text model did not return {expected_count} usable lines")


def enforce_word_limit(text, max_words):
    words = str(text or "").split()
    if len(words) <= max_words:
        return " ".join(words).strip()
    return " ".join(words[:max_words]).strip()


def build_tts_prompt(commentary_text, caster, prompt_style, tts_config):
    caster = normalize_caster_id(caster)
    voice_name = tts_config.voice_name
    speed = PLAY_BY_PLAY_SPEED if caster == CASTER0 else COLOR_SPEED
    if caster == CASTER0 and PLAY_BY_PLAY_VOICE_NAME:
        voice_name = PLAY_BY_PLAY_VOICE_NAME
    if caster == CASTER1 and COLOR_VOICE_NAME:
        voice_name = COLOR_VOICE_NAME

    return {
        "commentary": commentary_text,
        "caster": caster,
        "emotion": "",
        "speed": speed,
        "voice_name": voice_name,
        "prompt_style": prompt_style,
    }


def play_tts_prompt_interruptibly(tts_config, tts_prompt, interrupt_event):
    return stream_tts_playback_interruptibly_direct(tts_config, tts_prompt, interrupt_event)


def build_queue_item(*, commentary, caster, prompt_style, tag, payload_sequence, source):
    return {
        "id": next_item_sequence(),
        "created_at": now_stamp(),
        "commentary": " ".join(str(commentary or "").split()).strip(),
        "caster": normalize_caster_id(caster),
        "prompt_style": prompt_style,
        "tag": tag,
        "payload_sequence": payload_sequence,
        "source": source,
    }


def build_bundle(*, kind, items, payload_sequence, source):
    return {
        "id": next_bundle_sequence(),
        "created_at": now_stamp(),
        "created_monotonic": time.monotonic(),
        "kind": kind,
        "payload_sequence": payload_sequence,
        "source": source,
        "items": items,
        "interrupt_event": threading.Event(),
        "done_event": threading.Event(),
    }


def summarize_bundle(bundle):
    bundle = as_dict(bundle)
    return {
        "id": bundle.get("id"),
        "kind": bundle.get("kind"),
        "payload_sequence": bundle.get("payload_sequence"),
        "source": bundle.get("source"),
        "items": [
            {
                "id": item.get("id"),
                "tag": item.get("tag"),
                "caster": item.get("caster"),
                "prompt_style": item.get("prompt_style"),
                "commentary": item.get("commentary"),
            }
            for item in bundle.get("items", [])
        ],
    }


def write_queue_state_locked():
    write_pretty_json_file(
        PROMPT_QUEUE_STATE_PATH,
        {
            "updated_at": now_stamp(),
            "current_bundle": summarize_bundle(CURRENT_BUNDLE) if CURRENT_BUNDLE is not None else None,
            "current_item": copy.deepcopy(CURRENT_ITEM),
            "queued_bundles": [summarize_bundle(bundle) for bundle in PLAYBACK_QUEUE],
            "event_queue_overflow_started_at": EVENT_QUEUE_OVERFLOW_STARTED_AT,
        },
    )


def queued_event_bundle_count_locked():
    return sum(1 for bundle in PLAYBACK_QUEUE if bundle.get("kind") == "event")


def refresh_event_queue_overflow_timer_locked(now_monotonic=None):
    global EVENT_QUEUE_OVERFLOW_STARTED_AT
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    if queued_event_bundle_count_locked() >= 2:
        if EVENT_QUEUE_OVERFLOW_STARTED_AT is None:
            EVENT_QUEUE_OVERFLOW_STARTED_AT = now_monotonic
    else:
        EVENT_QUEUE_OVERFLOW_STARTED_AT = None


def seconds_until_event_queue_drop_locked(now_monotonic=None):
    if now_monotonic is None:
        now_monotonic = time.monotonic()
    refresh_event_queue_overflow_timer_locked(now_monotonic)
    if EVENT_QUEUE_OVERFLOW_STARTED_AT is None:
        return None
    elapsed = now_monotonic - EVENT_QUEUE_OVERFLOW_STARTED_AT
    return max(0.0, EVENT_QUEUE_DEQUEUE_SECONDS - elapsed)


def dequeue_one_overflow_event_bundle_if_due_locked(now_monotonic=None):
    global EVENT_QUEUE_OVERFLOW_STARTED_AT
    if now_monotonic is None:
        now_monotonic = time.monotonic()

    remaining = seconds_until_event_queue_drop_locked(now_monotonic)
    if remaining is None or remaining > 0:
        return None

    dropped = None
    kept = deque()
    for bundle in PLAYBACK_QUEUE:
        if dropped is None and bundle.get("kind") == "event":
            dropped = bundle
            continue
        kept.append(bundle)
    PLAYBACK_QUEUE.clear()
    PLAYBACK_QUEUE.extend(kept)

    if queued_event_bundle_count_locked() >= 2:
        EVENT_QUEUE_OVERFLOW_STARTED_AT = now_monotonic
    else:
        EVENT_QUEUE_OVERFLOW_STARTED_AT = None

    return dropped


def format_bundle_commentary(bundle, limit=220):
    items = [as_dict(item) for item in as_dict(bundle).get("items", [])]
    compact = " | ".join(
        slim_commentary(item.get("commentary"), limit=80)
        for item in items
        if item.get("commentary")
    )
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


def ensure_queue_monitor():
    global QUEUE_MONITOR_THREAD
    with QUEUE_CONDITION:
        if QUEUE_MONITOR_THREAD is not None and QUEUE_MONITOR_THREAD.is_alive():
            return

        def worker():
            while True:
                dropped = None
                with QUEUE_CONDITION:
                    timeout = seconds_until_event_queue_drop_locked()
                    if timeout is None:
                        QUEUE_CONDITION.wait()
                        continue
                    notified = QUEUE_CONDITION.wait(timeout=timeout)
                    if notified:
                        continue
                    dropped = dequeue_one_overflow_event_bundle_if_due_locked()
                    if dropped is not None:
                        write_queue_state_locked()
                if dropped is not None:
                    slim_log(
                        "queue trim",
                        commentary=format_bundle_commentary(dropped),
                        include_commentary=True,
                    )

        QUEUE_MONITOR_THREAD = threading.Thread(target=worker, daemon=True, name="gsi-v4-queue-monitor")
        QUEUE_MONITOR_THREAD.start()


def ensure_queue_worker(repo_root):
    global QUEUE_WORKER_THREAD, CURRENT_BUNDLE, CURRENT_ITEM
    with QUEUE_CONDITION:
        if QUEUE_WORKER_THREAD is not None and QUEUE_WORKER_THREAD.is_alive():
            return

        def worker():
            global CURRENT_BUNDLE, CURRENT_ITEM
            tts_config = build_tts_config(repo_root)
            while True:
                with QUEUE_CONDITION:
                    while True:
                        if PLAYBACK_QUEUE:
                            break
                        CURRENT_BUNDLE = None
                        CURRENT_ITEM = None
                        write_queue_state_locked()
                        QUEUE_CONDITION.wait()

                    CURRENT_BUNDLE = PLAYBACK_QUEUE.popleft()
                    refresh_event_queue_overflow_timer_locked()
                    CURRENT_ITEM = None
                    write_queue_state_locked()
                    QUEUE_CONDITION.notify_all()

                try:
                    for item in CURRENT_BUNDLE.get("items", []):
                        CURRENT_ITEM = {
                            "id": item.get("id"),
                            "tag": item.get("tag"),
                            "caster": item.get("caster"),
                            "commentary": item.get("commentary"),
                        }
                        with QUEUE_CONDITION:
                            write_queue_state_locked()

                        slim_log_tts_start(
                            item["id"],
                            tag=item["tag"],
                            caster=item["caster"],
                        )
                        playback = play_tts_prompt_interruptibly(
                            tts_config,
                            build_tts_prompt(
                                item["commentary"],
                                item["caster"],
                                item["prompt_style"],
                                tts_config,
                            ),
                            CURRENT_BUNDLE["interrupt_event"],
                        )
                        if playback.get("interrupted"):
                            slim_log_tts_finish(item["id"], "tts interrupted")
                            break
                        slim_log_tts_finish(item["id"], "tts end")
                except Exception as error:
                    if CURRENT_ITEM is not None:
                        slim_log_tts_finish(
                            CURRENT_ITEM["id"],
                            "tts failed",
                            commentary=str(error),
                            include_commentary=True,
                        )
                finally:
                    CURRENT_BUNDLE["done_event"].set()
                    with QUEUE_CONDITION:
                        CURRENT_BUNDLE = None
                        CURRENT_ITEM = None
                        refresh_event_queue_overflow_timer_locked()
                        write_queue_state_locked()
                        QUEUE_CONDITION.notify_all()

        QUEUE_WORKER_THREAD = threading.Thread(target=worker, daemon=True, name="gsi-v4-tts-worker")
        QUEUE_WORKER_THREAD.start()


def prepare_queue_for_event_bundle():
    dropped_bundles = []
    interrupted_current = None
    with QUEUE_CONDITION:
        kept = deque()
        for existing in PLAYBACK_QUEUE:
            if existing.get("kind") == "event":
                kept.append(existing)
            else:
                dropped_bundles.append(existing)
        PLAYBACK_QUEUE.clear()
        PLAYBACK_QUEUE.extend(kept)

        if CURRENT_BUNDLE is not None and CURRENT_BUNDLE.get("kind") != "event":
            CURRENT_BUNDLE["interrupt_event"].set()
            interrupted_current = summarize_bundle(CURRENT_BUNDLE)

        refresh_event_queue_overflow_timer_locked()
        write_queue_state_locked()
        QUEUE_CONDITION.notify_all()

    return dropped_bundles, interrupted_current


def enqueue_bundle(bundle, repo_root):
    ensure_queue_monitor()
    ensure_queue_worker(repo_root)
    with QUEUE_CONDITION:
        PLAYBACK_QUEUE.append(bundle)
        refresh_event_queue_overflow_timer_locked()
        write_queue_state_locked()
        QUEUE_CONDITION.notify_all()
    return []


def choose_chemistry_set():
    chemistry_sets = load_chemistry_sets()
    if not chemistry_sets:
        raise RuntimeError("no chemistry line sets available")
    chosen = random.choice(chemistry_sets)
    if isinstance(chosen, list):
        return chosen
    if isinstance(chosen, dict):
        lines = chosen.get("lines")
        if isinstance(lines, list):
            return lines
    raise RuntimeError("chemistry line set must be a list of line objects")


def process_event_wrapper(wrapper, repo_root, *, payload_sequence=None):
    text_config = build_text_llm_config(repo_root)
    system_prompt = build_event_system_prompt()
    user_prompt = build_event_user_prompt(wrapper)
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    analysis_caster = normalize_caster_id(wrapper_input.get("analysis_caster") or CASTER1)

    record = {
        "created_at": now_stamp(),
        "mode": "event_trigger",
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        result = request_chat_completion(text_config, system_prompt, user_prompt)
        parsed_lines = parse_json_line_array(result["raw_text"], 2)
        items = [
            build_queue_item(
                commentary=enforce_word_limit(parsed_lines[0], 5),
                caster=CASTER0,
                prompt_style="event_trigger",
                tag="event",
                payload_sequence=payload_sequence,
                source="event_trigger",
            ),
            build_queue_item(
                commentary=parsed_lines[1],
                caster=analysis_caster,
                prompt_style="event_analysis",
                tag="analysis",
                payload_sequence=payload_sequence,
                source="event_trigger",
            ),
        ]
        bundle = build_bundle(
            kind="event",
            items=items,
            payload_sequence=payload_sequence,
            source="event_trigger",
        )
        dropped_non_event_bundles, interrupted_current = prepare_queue_for_event_bundle()
        enqueue_bundle(bundle, repo_root)
        for item in items:
            slim_log(
                "prompt",
                tag=item["tag"],
                caster=item["caster"],
                commentary=item["commentary"],
                include_commentary=True,
            )
        record["status"] = "completed"
        record["llm"] = {
            "request": result["request"],
            "raw_text": result["raw_text"],
            "parsed_lines": parsed_lines,
        }
        record["queued_bundle"] = summarize_bundle(bundle)
        record["dropped_non_event_bundles"] = [summarize_bundle(bundle) for bundle in dropped_non_event_bundles]
        if interrupted_current is not None:
            record["interrupted_current"] = interrupted_current
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)

    finalized = strip_empty(record)
    append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized)
    write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized)
    return finalized


def process_interval_wrapper(wrapper, repo_root, *, payload_sequence=None, interval_mode=None):
    if interval_mode is None:
        interval_mode = as_dict(as_dict(wrapper).get("input")).get("request", {}).get("mode") or next_idle_mode()

    record = {
        "created_at": now_stamp(),
        "mode": interval_mode,
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        if interval_mode == "chemistry":
            chosen = choose_chemistry_set()
            items = []
            for line in chosen if isinstance(chosen, list) else []:
                line = as_dict(line)
                items.append(
                    build_queue_item(
                        commentary=line.get("text") or "",
                        caster=line.get("caster") or CASTER0,
                        prompt_style="chemistry",
                        tag="chemistry",
                        payload_sequence=payload_sequence,
                        source="chemistry",
                    )
                )
            bundle = build_bundle(
                kind="chemistry",
                items=items,
                payload_sequence=payload_sequence,
                source="chemistry",
            )
            enqueue_bundle(bundle, repo_root)
            for item in items:
                slim_log(
                    "prompt",
                    tag=item["tag"],
                    caster=item["caster"],
                    commentary=item["commentary"],
                    include_commentary=True,
                )
            record["status"] = "completed"
            record["chemistry_set"] = copy.deepcopy(chosen)
            record["queued_bundle"] = summarize_bundle(bundle)
        else:
            text_config = build_text_llm_config(repo_root)
            system_prompt = build_idle_analysis_system_prompt()
            user_prompt = build_idle_analysis_user_prompt(wrapper)
            result = request_chat_completion(text_config, system_prompt, user_prompt)
            parsed_lines = parse_json_line_array(result["raw_text"], 3)
            items = [
                build_queue_item(
                    commentary=parsed_lines[0],
                    caster=CASTER0,
                    prompt_style="idle_analysis",
                    tag="idle",
                    payload_sequence=payload_sequence,
                    source="idle_analysis",
                ),
                build_queue_item(
                    commentary=parsed_lines[1],
                    caster=CASTER1,
                    prompt_style="idle_analysis",
                    tag="idle",
                    payload_sequence=payload_sequence,
                    source="idle_analysis",
                ),
                build_queue_item(
                    commentary=parsed_lines[2],
                    caster=CASTER0,
                    prompt_style="idle_analysis",
                    tag="idle",
                    payload_sequence=payload_sequence,
                    source="idle_analysis",
                ),
            ]
            bundle = build_bundle(
                kind="idle_analysis",
                items=items,
                payload_sequence=payload_sequence,
                source="idle_analysis",
            )
            enqueue_bundle(bundle, repo_root)
            for item in items:
                slim_log(
                    "prompt",
                    tag=item["tag"],
                    caster=item["caster"],
                    commentary=item["commentary"],
                    include_commentary=True,
                )
            record["status"] = "completed"
            record["llm"] = {
                "request": result["request"],
                "raw_text": result["raw_text"],
                "parsed_lines": parsed_lines,
            }
            record["queued_bundle"] = summarize_bundle(bundle)
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)

    finalized = strip_empty(record)
    append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized)
    write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized)
    return finalized
