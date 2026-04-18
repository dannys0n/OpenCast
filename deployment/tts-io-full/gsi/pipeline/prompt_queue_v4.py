import copy
import json
import os
import random
import re
import sys
import tempfile
import threading
import time
from functools import lru_cache
from collections import deque
from datetime import datetime
from pathlib import Path

from gsi_prompt_pipeline_v2 import as_dict
from text_llm_client import build_config as build_text_llm_config
from text_llm_client import request_chat_completion
from tts_client import (
    build_config as build_tts_config,
    fetch_tts_audio_to_file,
    open_play_process,
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
FEW_SHOT_EXAMPLES_PATH = SCRIPT_DIR / "few_shot_examples.json"
PROMPT_CONFIG_PATH = SCRIPT_DIR / "prompt_config_v4.json"
CHEMISTRY_LINES_PATH = SCRIPT_DIR / "chemistry_lines_v4.json"

QUEUE_LOCK = threading.Lock()
QUEUE_CONDITION = threading.Condition(QUEUE_LOCK)
PLAYBACK_QUEUE = deque()
CURRENT_PLAYBACK = None
QUEUE_WORKER_THREAD = None
INTERVAL_MODE_INDEX = 0
ITEM_SEQUENCE = 0
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


PLAY_BY_PLAY_VOICE_NAME = env_text("V3_PLAY_BY_PLAY_VOICE_NAME", env_text("PLAY_BY_PLAY_VOICE_NAME", ""))
COLOR_VOICE_NAME = env_text("V3_COLOR_VOICE_NAME", env_text("COLOR_VOICE_NAME", ""))
PLAY_BY_PLAY_SPEED = env_float("V3_PLAY_BY_PLAY_SPEED", 1.08)
COLOR_SPEED = env_float("V3_COLOR_SPEED", 1.0)


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


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
        "followup": ANSI_BLUE,
        "idle": ANSI_MAGENTA,
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


def format_trimmed_items(items, limit=220):
    rendered = []
    for item in items:
        tag = item.get("tag")
        text = slim_commentary(item.get("commentary") or "", limit=90)
        if tag:
            rendered.append(f"[{tag}] {text}")
        else:
            rendered.append(text)
    compact = " | ".join(part for part in rendered if part)
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3].rstrip()}..."


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
    prefix = f"[+{delta:0.3f}s] {action}"
    prefix = f"{colorize(prefix, ANSI_DIM)}"
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


def slim_log_tts_start(item_id, *, tag=None, caster=None, commentary=None):
    global OPEN_TTS_LOG_ITEM_ID, OPEN_TTS_LOG_STARTED_AT
    with LOG_OUTPUT_LOCK:
        _close_open_tts_log_line_locked()
        print(_build_slim_log_text("tts start", tag=tag, caster=caster, commentary=commentary), end="", flush=True)
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


def strip_empty(value):
    if isinstance(value, dict):
        cleaned = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [strip_empty(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    return value


def load_prompt_config():
    if not PROMPT_CONFIG_PATH.exists():
        return {
            "interval_seconds": 10,
            "event_instruction": (
                "You are generating Counter-Strike 2 caster lines for live TTS. "
                "Return plain text only. "
                "For event prompts, return exactly 2 lines. "
                f"Line 1 is the {CASTER0} event trigger call. "
                f"Line 2 is a short {CASTER1} follow-up line. "
                "No labels. No JSON. No markdown."
            ),
            "interval_instruction": (
                "You are generating Counter-Strike 2 idle caster lines for live TTS. "
                "Return plain text only. "
                "Return exactly 3 short lines. "
                "No labels. No JSON. No markdown."
            ),
            "event_system_prompt_template": (
                "{event_instruction} "
                "This is Counter-Strike 2. "
                "Line 1 should fit {caster0}'s rapid event-call style and be extremely short, ideally 2 to 5 words and never exceed 8 words. "
                "Line 2 should fit {caster1}'s short follow-up style and stay speakable. "
                "Treat tactical_facts as structured tactical metadata, not as wording to repeat. "
                "Use previous_events and current_events for recency, and use Tactical context for persistent state. "
                "Line 2 may use Tactical context and Previous events, but only when the point is clearly supported by the input. "
                "Prefer one tactical implication instead of restating the event. "
                "Do not quote enum labels verbatim; translate them into natural commentary. "
                "Vary phrasing rather than reusing canned wording. "
                "If the tactical confidence is low, prefer cautious phrasing over certainty. "
                "If the event is a kill and the killer has round_kills of 2 or more, prefer double, triple, quad, or ace style phrasing when appropriate. "
                "If the event is grenade_detonated, almost always mention detonation_callout. "
                "Few-shot JSON examples:\n{few_shots_json}"
            ),
            "interval_system_prompt_idle_template": (
                "{interval_instruction} "
                "This is Counter-Strike 2. "
                "Be concise, grounded, and avoid repeating the same context. "
                "Treat tactical_facts as structured tactical metadata, not as ready-made commentary. "
                "Use Tactical context to sound live rather than generic. "
                "Pick one or two signals that matter most instead of trying to explain everything. "
                "Do not quote enum labels verbatim; translate them into natural commentary. "
                "Vary phrasing rather than leaning on fixed wording. "
                "Prefer one concrete observation per line. If confidence is low, stay tentative. "
                "Generate 3 understated {caster1} lines that avoid repeating the same point. "
                "Each line should be a single short sentence. "
                "Few-shot JSON examples:\n{few_shots_json}"
            ),
            "interval_system_prompt_conversation_template": (
                "{interval_instruction} "
                "This is Counter-Strike 2. "
                "Be concise, grounded, and avoid repeating the same context. "
                "Treat tactical_facts as structured tactical metadata, not as ready-made commentary. "
                "Use Tactical context to sound live rather than generic. "
                "Pick one or two signals that matter most instead of trying to explain everything. "
                "Do not quote enum labels verbatim; translate them into natural commentary. "
                "Vary phrasing rather than leaning on fixed wording. "
                "Prefer one concrete observation per line. If confidence is low, stay tentative. "
                "Generate a tiny 3-line conversation between {caster0} and {caster1}. "
                "The lines should sound like a short back-and-forth, but return commentary lines only with no speaker labels. "
                "Each line should be a single short sentence. "
                "Few-shot JSON examples:\n{few_shots_json}"
            ),
        }

    try:
        loaded = json.loads(PROMPT_CONFIG_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}

    return loaded if isinstance(loaded, dict) else {}


def load_few_shot_examples():
    if not FEW_SHOT_EXAMPLES_PATH.exists():
        return []

    try:
        loaded = json.loads(FEW_SHOT_EXAMPLES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []

    return loaded if isinstance(loaded, list) else []


class SafePromptFormatDict(dict):
    def __missing__(self, key):
        return ""


def render_prompt_template(template, values):
    template = str(template or "").strip()
    if not template:
        return ""
    return template.format_map(SafePromptFormatDict(values)).strip()


def reset_prompt_runtime_state():
    global PLAYBACK_QUEUE, CURRENT_PLAYBACK, QUEUE_WORKER_THREAD, INTERVAL_MODE_INDEX, ITEM_SEQUENCE
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
        CURRENT_PLAYBACK = None
        QUEUE_WORKER_THREAD = None
        INTERVAL_MODE_INDEX = 0
        ITEM_SEQUENCE = 0


def next_item_sequence():
    global ITEM_SEQUENCE
    with QUEUE_CONDITION:
        ITEM_SEQUENCE += 1
        return ITEM_SEQUENCE


def trim_few_shot_example(example):
    example_input = as_dict(example.get("input"))
    context = as_dict(example_input.get("context"))
    if not context:
        legacy_match_context = as_dict(example_input.get("match_context"))
        context = {
            "score": legacy_match_context.get("score"),
            "alive_players": legacy_match_context.get("alive_players"),
        }
    request = as_dict(example_input.get("request"))
    if not request:
        request = {"mode": "unknown"}

    example = as_dict(example)
    return strip_empty(
        {
            "input": {
                "context": context,
                "previous_events": example_input.get("previous_events"),
                "current_events": example_input.get("current_events"),
                "request": request,
            },
            "output": {
                "commentary": as_dict(example.get("output")).get("commentary"),
                "prompt_style": as_dict(example.get("output")).get("prompt_style"),
                "caster": normalize_caster_id(as_dict(example.get("output")).get("caster")),
            },
        }
    )


def example_primary_event(example):
    example_input = as_dict(as_dict(example).get("input"))
    return as_dict(primary_event(example_input.get("current_events", [])))


def few_shot_sort_key(example, target_event_type):
    example_event = example_primary_event(example)
    event_type = example_event.get("event_type")

    same_type_rank = 0 if event_type == target_event_type else 1

    if target_event_type == "kill":
        round_kills = as_dict(example_event.get("killer")).get("round_kills")
        try:
            round_kills_rank = int(round_kills)
        except (TypeError, ValueError):
            round_kills_rank = 999
        return (same_type_rank, round_kills_rank, event_type or "")

    if target_event_type == "grenade_detonated":
        grenade_order = {
            "smoke": 0,
            "flashbang": 1,
            "frag": 2,
            "molotov": 3,
            "incendiary": 4,
            "decoy": 5,
        }
        return (same_type_rank, grenade_order.get(example_event.get("grenade_type"), 999), event_type or "")

    return (same_type_rank, event_type or "")


def select_few_shot_examples(*, casters, prompt_styles, current_events=None, limit=4):
    normalized_casters = {normalize_caster_id(caster) for caster in casters}
    selected = []
    for example in load_few_shot_examples():
        output = as_dict(example.get("output"))
        if normalize_caster_id(output.get("caster")) not in normalized_casters:
            continue
        if output.get("prompt_style") not in prompt_styles:
            continue
        selected.append(example)

    target_event_type = as_dict(primary_event(current_events or [])).get("event_type")
    if target_event_type:
        selected.sort(key=lambda example: few_shot_sort_key(example, target_event_type))
        if target_event_type == "kill":
            deduped = []
            seen_round_kills = set()
            for example in selected:
                round_kills = as_dict(example_primary_event(example).get("killer")).get("round_kills")
                if round_kills in seen_round_kills:
                    continue
                seen_round_kills.add(round_kills)
                deduped.append(example)
            selected = deduped

    return [trim_few_shot_example(example) for example in selected[:limit]]


def build_global_context(context):
    context = as_dict(context)
    return strip_empty(
        {
            "bomb_state": context.get("bomb_state"),
            "score": context.get("score"),
            "alive_players": context.get("alive_players"),
        }
    )


def build_tactical_prompt_context(wrapper_input):
    return strip_empty(
        {
            "global_context": build_global_context(wrapper_input.get("context")),
            "tactical_facts": wrapper_input.get("derived_tactical_summary"),
        }
    )


def build_idle_prompt_context(wrapper_input):
    return strip_empty(
        {
            "global_context": build_global_context(wrapper_input.get("context")),
            "tactical_facts": wrapper_input.get("derived_tactical_summary"),
        }
    )


@lru_cache(maxsize=1)
def load_chemistry_line_sets():
    if not CHEMISTRY_LINES_PATH.exists():
        return []
    payload = json.loads(CHEMISTRY_LINES_PATH.read_text(encoding="utf-8"))
    return [entry for entry in payload if isinstance(entry, list) and entry]


def choose_chemistry_line_set():
    line_sets = load_chemistry_line_sets()
    if not line_sets:
        raise RuntimeError(f"chemistry lines file is empty: {CHEMISTRY_LINES_PATH}")
    return copy.deepcopy(random.choice(line_sets))


def primary_event(current_events):
    current_events = [as_dict(event) for event in current_events]
    if not current_events:
        return {}

    priorities = {
        "kill": 100,
        "kill_cluster": 95,
        "player_scored_kill": 90,
        "player_death": 85,
        "grenade_detonated": 80,
        "grenade_thrown": 70,
        "bomb_event": 60,
        "round_result": 50,
        "game_over": 40,
        "team_counter": 10,
    }
    return max(current_events, key=lambda event: priorities.get(event.get("event_type"), 0))


def build_focused_context(current_events):
    event = primary_event(current_events)
    event_type = event.get("event_type")

    if event_type == "kill":
        return {"focused_player": as_dict(event.get("killer"))}
    if event_type in {"player_scored_kill", "player_death"}:
        return {"focused_player": as_dict(event.get("player"))}
    if event_type in {"grenade_thrown", "grenade_detonated"}:
        return {"focused_player": as_dict(event.get("owner_player"))}

    return {}


def build_event_system_prompt(current_events):
    few_shots = select_few_shot_examples(
        casters={CASTER0, CASTER1},
        prompt_styles={"play_by_play_event", "play_by_play_follow_up"},
        current_events=current_events,
        limit=3,
    )
    config = load_prompt_config()
    return render_prompt_template(
        config.get("event_system_prompt_template", ""),
        {
            "event_instruction": config.get("event_instruction", "").strip(),
            "caster0": CASTER0,
            "caster1": CASTER1,
            "few_shots_json": json.dumps(few_shots, indent=2, sort_keys=True),
        },
    )


def build_interval_system_prompt(conversation_mode):
    few_shots = select_few_shot_examples(
        casters={CASTER1, CASTER0},
        prompt_styles={"idle_color"},
        limit=2,
    )
    config = load_prompt_config()
    template_key = (
        "interval_system_prompt_conversation_template"
        if conversation_mode
        else "interval_system_prompt_idle_template"
    )
    return render_prompt_template(
        config.get(template_key, ""),
        {
            "interval_instruction": config.get("interval_instruction", "").strip(),
            "caster0": CASTER0,
            "caster1": CASTER1,
            "few_shots_json": json.dumps(few_shots, indent=2, sort_keys=True),
        },
    )


def build_event_user_prompt(wrapper):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "previous_events": wrapper_input.get("previous_events"),
            "current_events": wrapper_input.get("current_events"),
            "derived_tactical_summary": wrapper_input.get("derived_tactical_summary"),
            "request": wrapper_input.get("request"),
        }
    )
    return (
        "Generate exactly 2 lines.\n"
        f"Line 1: very short {CASTER0} event trigger call using only Focused context and Current events.\n"
        f"Line 2: short {CASTER1} follow-up line that may use Tactical context.\n"
        "Use tactical_facts as facts to reason from, not text to copy.\n"
        "Do not add labels or numbering.\n\n"
        "Focused context:\n"
        f"{json.dumps(build_focused_context(wrapper_input.get('current_events', [])), indent=2, sort_keys=True)}\n\n"
        "Tactical facts:\n"
        f"{json.dumps(build_tactical_prompt_context(wrapper_input), indent=2, sort_keys=True)}\n\n"
        "Event input:\n"
        f"{json.dumps(prompt_input, indent=2, sort_keys=True)}"
    )


def build_interval_user_prompt(wrapper, conversation_mode):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "derived_tactical_summary": wrapper_input.get("derived_tactical_summary"),
            "request": wrapper_input.get("request"),
        }
    )
    mode_text = (
        f"Generate exactly 3 lines for a short {CASTER0}/{CASTER1} idle exchange."
        if conversation_mode
        else f"Generate exactly 3 short idle {CASTER1} lines."
    )
    return (
        f"{mode_text}\n"
        "Use the Live context below.\n"
        "Use tactical_facts as facts to reason from, not text to copy.\n"
        "Do not add labels or numbering.\n\n"
        "Live context:\n"
        f"{json.dumps(build_idle_prompt_context(wrapper_input), indent=2, sort_keys=True)}\n\n"
        "Prompt input:\n"
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
        if candidate.lower().startswith("json"):
            continue
        cleaned.append(candidate)
        if len(cleaned) >= expected_max:
            break

    if not cleaned:
        raise RuntimeError("text model returned no usable commentary lines")

    return cleaned


def split_compound_event_lines(lines, expected_max):
    split_lines = []
    for line in lines:
        sentences = re.split(r"(?<=[.!?])\s+", str(line).strip())
        for sentence in sentences:
            candidate = sentence.strip()
            if not candidate:
                continue
            split_lines.append(candidate)
            if len(split_lines) >= expected_max:
                return split_lines
    return split_lines


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


def write_queue_state_locked():
    queue_snapshot = [
        {
            "id": item["id"],
            "tag": item["tag"],
            "caster": item["caster"],
            "prompt_style": item["prompt_style"],
            "commentary": item["commentary"],
            "payload_sequence": item.get("payload_sequence"),
            "source": item.get("source"),
        }
        for item in PLAYBACK_QUEUE
    ]
    current_snapshot = None
    if CURRENT_PLAYBACK is not None:
        current_snapshot = {
            "id": CURRENT_PLAYBACK["id"],
            "tag": CURRENT_PLAYBACK["tag"],
            "caster": CURRENT_PLAYBACK["caster"],
            "prompt_style": CURRENT_PLAYBACK["prompt_style"],
            "commentary": CURRENT_PLAYBACK["commentary"],
            "payload_sequence": CURRENT_PLAYBACK.get("payload_sequence"),
            "source": CURRENT_PLAYBACK.get("source"),
        }
    write_pretty_json_file(
        PROMPT_QUEUE_STATE_PATH,
        {
            "updated_at": now_stamp(),
            "current": current_snapshot,
            "queued": queue_snapshot,
        },
    )


def play_tts_prompt_interruptibly(tts_config, tts_prompt, interrupt_event):
    return stream_tts_playback_interruptibly_direct(tts_config, tts_prompt, interrupt_event)


def ensure_queue_worker(repo_root):
    global QUEUE_WORKER_THREAD
    with QUEUE_CONDITION:
        if QUEUE_WORKER_THREAD is not None and QUEUE_WORKER_THREAD.is_alive():
            return

        def worker():
            global CURRENT_PLAYBACK
            tts_config = build_tts_config(repo_root)
            while True:
                with QUEUE_CONDITION:
                    while not PLAYBACK_QUEUE:
                        CURRENT_PLAYBACK = None
                        write_queue_state_locked()
                        QUEUE_CONDITION.wait()
                    CURRENT_PLAYBACK = PLAYBACK_QUEUE.popleft()
                    write_queue_state_locked()

                try:
                    slim_log_tts_start(
                        CURRENT_PLAYBACK["id"],
                        tag=CURRENT_PLAYBACK["tag"],
                        caster=CURRENT_PLAYBACK["caster"],
                    )
                    playback = play_tts_prompt_interruptibly(
                        tts_config,
                        build_tts_prompt(
                            CURRENT_PLAYBACK["commentary"],
                            CURRENT_PLAYBACK["caster"],
                            CURRENT_PLAYBACK["prompt_style"],
                            tts_config,
                        ),
                        CURRENT_PLAYBACK["interrupt_event"],
                    )
                except Exception as error:
                    CURRENT_PLAYBACK["playback_error"] = str(error)
                    CURRENT_PLAYBACK["playback_result"] = {"failed": True}
                    slim_log_tts_finish(
                        CURRENT_PLAYBACK["id"],
                        "tts failed",
                        commentary=str(error),
                        include_commentary=True,
                    )
                else:
                    CURRENT_PLAYBACK["playback_result"] = playback
                    if playback.get("interrupted"):
                        slim_log_tts_finish(CURRENT_PLAYBACK["id"], "tts interrupted")
                    else:
                        slim_log_tts_finish(CURRENT_PLAYBACK["id"], "tts end")
                finally:
                    CURRENT_PLAYBACK["done_event"].set()
                    with QUEUE_CONDITION:
                        CURRENT_PLAYBACK = None
                        write_queue_state_locked()

        QUEUE_WORKER_THREAD = threading.Thread(target=worker, daemon=True, name="gsi-v3-tts-worker")
        QUEUE_WORKER_THREAD.start()


def enqueue_prompt_items(items, repo_root):
    ensure_queue_worker(repo_root)
    with QUEUE_CONDITION:
        for item in items:
            PLAYBACK_QUEUE.append(item)

        write_queue_state_locked()
        QUEUE_CONDITION.notify_all()

    return []


def prepare_queue_for_event_trigger():
    dropped_items = []
    interrupted_current = None
    with QUEUE_CONDITION:
        kept = deque()
        for existing in PLAYBACK_QUEUE:
            if existing["tag"] == "event":
                kept.append(existing)
            else:
                dropped_items.append(existing)
        PLAYBACK_QUEUE.clear()
        PLAYBACK_QUEUE.extend(kept)

        if CURRENT_PLAYBACK is not None and CURRENT_PLAYBACK["tag"] != "event":
            CURRENT_PLAYBACK["interrupt_event"].set()
            interrupted_current = {
                "id": CURRENT_PLAYBACK["id"],
                "tag": CURRENT_PLAYBACK["tag"],
                "commentary": CURRENT_PLAYBACK["commentary"],
            }

        write_queue_state_locked()

    return dropped_items, interrupted_current


def build_queue_item(*, commentary, caster, prompt_style, tag, payload_sequence, source):
    return {
        "id": next_item_sequence(),
        "created_at": now_stamp(),
        "commentary": commentary,
        "caster": normalize_caster_id(caster),
        "prompt_style": prompt_style,
        "tag": tag,
        "payload_sequence": payload_sequence,
        "source": source,
        "interrupt_event": threading.Event(),
        "done_event": threading.Event(),
    }


def is_spectator_mode(snapshot):
    snapshot = as_dict(snapshot)
    return bool(as_dict(snapshot.get("allplayers")))


def should_ignore_event_prompt(wrapper, snapshot):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    current_events = [as_dict(event) for event in wrapper_input.get("current_events", [])]
    if not current_events:
        return False

    if all(event.get("event_type") == "grenade_thrown" for event in current_events) and is_spectator_mode(snapshot):
        return True

    return False


def process_event_wrapper(wrapper, repo_root, *, payload_sequence=None, snapshot=None):
    if should_ignore_event_prompt(wrapper, snapshot):
        return None

    text_config = build_text_llm_config(repo_root)
    current_events = as_dict(as_dict(wrapper).get("input")).get("current_events", [])
    system_prompt = build_event_system_prompt(current_events)
    user_prompt = build_event_user_prompt(wrapper)
    record = {
        "created_at": now_stamp(),
        "mode": "event",
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        result = request_chat_completion(text_config, system_prompt, user_prompt)
        lines = extract_commentary_lines(result["raw_text"], expected_max=4)
        lines = split_compound_event_lines(lines, expected_max=4)
        dropped = []
        interrupted_current = None
        if lines:
            dropped, interrupted_current = prepare_queue_for_event_trigger()
            if dropped:
                slim_log(
                    "queue trim",
                    commentary=format_trimmed_items(dropped),
                    include_commentary=True,
                )
        items = []
        if lines:
            items.append(
                build_queue_item(
                    commentary=lines[0],
                    caster=CASTER0,
                    prompt_style="play_by_play_event",
                    tag="event",
                    payload_sequence=payload_sequence,
                    source="event",
                )
            )
        if len(lines) > 1:
            for followup_line in lines[1:]:
                items.append(
                    build_queue_item(
                        commentary=followup_line,
                        caster=CASTER1,
                        prompt_style="play_by_play_follow_up",
                        tag="followup",
                        payload_sequence=payload_sequence,
                        source="event",
                    )
                )

        enqueue_prompt_items(items, repo_root) if items else None
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
            "lines": lines,
        }
        record["queued_items"] = [
            {
                "id": item["id"],
                "tag": item["tag"],
                "caster": item["caster"],
                "prompt_style": item["prompt_style"],
                "commentary": item["commentary"],
            }
            for item in items
        ]
        record["dropped_items"] = [
            {
                "id": item["id"],
                "tag": item["tag"],
                "commentary": item["commentary"],
            }
            for item in dropped
        ]
        if interrupted_current is not None:
            record["interrupted_current"] = interrupted_current
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)

    finalized = strip_empty(record)
    append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized)
    write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized)
    return finalized


def join_commentary_lines(lines):
    joined = " ".join(line.strip() for line in lines if str(line).strip())
    return " ".join(joined.split()).strip()


def next_interval_mode():
    global INTERVAL_MODE_INDEX
    with QUEUE_CONDITION:
        mode = "idle_color"
        if INTERVAL_MODE_INDEX % 2 == 1:
            mode = "idle_conversation"
        INTERVAL_MODE_INDEX += 1
        return mode


def process_interval_wrapper(wrapper, repo_root, *, payload_sequence=None, interval_mode=None):
    if interval_mode is None:
        interval_mode = as_dict(as_dict(wrapper).get("input")).get("request", {}).get("mode") or next_interval_mode()
    conversation_mode = interval_mode == "idle_conversation"
    record = {
        "created_at": now_stamp(),
        "mode": interval_mode,
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        items = []
        if conversation_mode:
            selected_set = choose_chemistry_line_set()
            lines = []
            for line_entry in selected_set[:3]:
                commentary = " ".join(str(as_dict(line_entry).get("text") or "").split()).strip()
                if not commentary:
                    continue
                caster = normalize_caster_id(as_dict(line_entry).get("caster") or CASTER1)
                lines.append(commentary)
                items.append(
                    build_queue_item(
                        commentary=commentary,
                        caster=caster,
                        prompt_style="idle_color",
                        tag="idle",
                        payload_sequence=payload_sequence,
                        source=interval_mode,
                    )
                )
            record["chemistry_set"] = selected_set
        else:
            text_config = build_text_llm_config(repo_root)
            system_prompt = build_interval_system_prompt(conversation_mode)
            user_prompt = build_interval_user_prompt(wrapper, conversation_mode)
            result = request_chat_completion(text_config, system_prompt, user_prompt)
            lines = extract_commentary_lines(result["raw_text"], expected_max=3)
            commentary = join_commentary_lines(lines)
            if commentary:
                items.append(
                    build_queue_item(
                        commentary=commentary,
                        caster=CASTER1,
                        prompt_style="idle_color",
                        tag="idle",
                        payload_sequence=payload_sequence,
                        source=interval_mode,
                    )
                )

        dropped = enqueue_prompt_items(items, repo_root) if items else []
        for item in items:
            slim_log(
                "prompt",
                tag=item["tag"],
                caster=item["caster"],
                commentary=item["commentary"],
                include_commentary=True,
            )
        record["status"] = "completed"
        if conversation_mode:
            record["lines"] = lines
        else:
            record["llm"] = {
                "request": result["request"],
                "raw_text": result["raw_text"],
                "lines": lines,
            }
        record["queued_items"] = [
            {
                "id": item["id"],
                "tag": item["tag"],
                "caster": item["caster"],
                "prompt_style": item["prompt_style"],
                "commentary": item["commentary"],
            }
            for item in items
        ]
        record["dropped_items"] = [
            {
                "id": item["id"],
                "tag": item["tag"],
                "commentary": item["commentary"],
            }
            for item in dropped
        ]
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)

    finalized = strip_empty(record)
    append_pretty_json_record(PROMPT_RUNTIME_HISTORY_PATH, finalized)
    write_pretty_json_file(PROMPT_RUNTIME_LATEST_PATH, finalized)
    return finalized
