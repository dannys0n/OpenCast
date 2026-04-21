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

from gsi_prompt_pipeline_v2 import as_dict, normalize_team
from text_llm_client import build_config as build_text_llm_config
from text_llm_client import request_chat_completion
from tts_client import (
    build_config as build_tts_config,
    fetch_tts_audio_to_file,
    open_play_process,
    stream_prefetched_tts_playback_interruptibly,
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
STATE_DIR = SCRIPT_DIR / ".state" / "v5"
PROMPT_RUNTIME_HISTORY_PATH = STATE_DIR / "prompt_runtime_pretty.jsonl"
PROMPT_RUNTIME_LATEST_PATH = STATE_DIR / "prompt_runtime_latest.json"
PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"
TTS_FIRST_PCM_STATS_PATH = STATE_DIR / "tts_first_pcm_stats.json"
FEW_SHOT_EXAMPLES_PATH = SCRIPT_DIR / "few_shot_examples.json"
PROMPT_CONFIG_PATH = SCRIPT_DIR / "prompt_config_v5.json"
CHEMISTRY_LINES_PATH = SCRIPT_DIR / "chemistry_lines_v5.json"

QUEUE_LOCK = threading.Lock()
QUEUE_CONDITION = threading.Condition(QUEUE_LOCK)
PLAYBACK_QUEUE = deque()
CURRENT_PLAYBACK = None
QUEUE_WORKER_THREAD = None
INTERVAL_MODE_INDEX = 0
ITEM_SEQUENCE = 0
LAST_LOG_MONOTONIC = None
LOG_OUTPUT_LOCK = threading.Lock()
TTS_STATS_LOCK = threading.Lock()
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


PLAY_BY_PLAY_VOICE_NAME = env_text(
    "V5_PLAY_BY_PLAY_VOICE_NAME",
    env_text(
        "PLAY_BY_PLAY_VOICE_NAME",
        "clone:announcer_e0",
    ),
)
COLOR_VOICE_NAME = env_text(
    "V5_COLOR_VOICE_NAME",
    env_text(
        "COLOR_VOICE_NAME",
        "clone:turret_e0",
    ),
)
PLAY_BY_PLAY_SPEED = env_float("V5_PLAY_BY_PLAY_SPEED", 1.08)
COLOR_SPEED = env_float("V5_COLOR_SPEED", 1.0)
TEXT_MODEL_NAME = env_text(
    "V5_MODEL_NAME",
    "hf.co/Dannys0n/Qwen3-1.7B-cs2-commentators:Q4_K_M",
)


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def build_v5_text_llm_config(repo_root):
    config = build_text_llm_config(repo_root)
    if TEXT_MODEL_NAME:
        config.model_name = TEXT_MODEL_NAME
    return config


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
        TTS_FIRST_PCM_STATS_PATH,
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


def load_json_file(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return copy.deepcopy(default)
    return loaded if isinstance(loaded, type(default)) else copy.deepcopy(default)


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return float(ordered[mid])
    return float((ordered[mid - 1] + ordered[mid]) / 2.0)


def compute_filtered_latency_stats(samples):
    samples = [float(sample) for sample in samples if isinstance(sample, (int, float)) and sample >= 0.0]
    if not samples:
        return {
            "raw_count": 0,
            "filtered_count": 0,
            "average_seconds": None,
            "median_seconds": None,
            "kept_samples_seconds": [],
        }

    median = _median(samples)
    deviations = [abs(sample - median) for sample in samples]
    mad = _median(deviations)

    if len(samples) >= 5 and mad is not None and mad > 0:
        filtered = []
        for sample in samples:
            modified_z = 0.6745 * abs(sample - median) / mad
            if modified_z <= 3.5:
                filtered.append(sample)
    elif len(samples) >= 10:
        ordered = sorted(samples)
        trim = max(1, int(len(ordered) * 0.1))
        filtered = ordered[trim:-trim] if len(ordered) > trim * 2 else ordered
    else:
        filtered = list(samples)

    if not filtered:
        filtered = list(samples)

    return {
        "raw_count": len(samples),
        "filtered_count": len(filtered),
        "average_seconds": sum(filtered) / len(filtered),
        "median_seconds": _median(filtered),
        "kept_samples_seconds": filtered,
    }


def record_tts_first_pcm_latency(fetch_result):
    if not isinstance(fetch_result, dict):
        return
    if fetch_result.get("_latency_recorded"):
        return

    latency = fetch_result.get("first_pcm_latency_seconds")
    if not isinstance(latency, (int, float)) or latency < 0.0:
        return

    with TTS_STATS_LOCK:
        payload = load_json_file(TTS_FIRST_PCM_STATS_PATH, {})
        samples = payload.get("recent_first_pcm_latency_seconds")
        if not isinstance(samples, list):
            samples = []
        samples.append(float(latency))
        samples = samples[-100:]
        stats = compute_filtered_latency_stats(samples)

        text_samples = payload.get("recent_text_generation_completion_latency_seconds")
        if not isinstance(text_samples, list):
            text_samples = []
        text_stats = compute_filtered_latency_stats(text_samples)

        record = {
            "updated_at": now_stamp(),
            "recent_first_pcm_latency_seconds": [round(sample, 4) for sample in samples],
            "raw_sample_count": stats["raw_count"],
            "filtered_sample_count": stats["filtered_count"],
            "average_first_pcm_latency_seconds": round(stats["average_seconds"], 4)
            if stats["average_seconds"] is not None
            else None,
            "median_first_pcm_latency_seconds": round(stats["median_seconds"], 4)
            if stats["median_seconds"] is not None
            else None,
            "recent_text_generation_completion_latency_seconds": [
                round(sample, 4) for sample in text_samples
            ],
            "text_generation_raw_sample_count": text_stats["raw_count"],
            "text_generation_filtered_sample_count": text_stats["filtered_count"],
            "average_text_generation_completion_latency_seconds": round(
                text_stats["average_seconds"], 4
            )
            if text_stats["average_seconds"] is not None
            else None,
            "median_text_generation_completion_latency_seconds": round(
                text_stats["median_seconds"], 4
            )
            if text_stats["median_seconds"] is not None
            else None,
            "outlier_strategy": "median_mad_then_trimmed_fallback",
        }
        write_pretty_json_file(TTS_FIRST_PCM_STATS_PATH, record)

    fetch_result["_latency_recorded"] = True


def record_text_generation_completion_latency(result):
    if not isinstance(result, dict):
        return
    if result.get("_text_generation_latency_recorded"):
        return

    latency = result.get("text_generation_completion_latency_seconds")
    if not isinstance(latency, (int, float)) or latency < 0.0:
        return

    with TTS_STATS_LOCK:
        payload = load_json_file(TTS_FIRST_PCM_STATS_PATH, {})

        first_pcm_samples = payload.get("recent_first_pcm_latency_seconds")
        if not isinstance(first_pcm_samples, list):
            first_pcm_samples = []
        first_pcm_stats = compute_filtered_latency_stats(first_pcm_samples)

        text_samples = payload.get("recent_text_generation_completion_latency_seconds")
        if not isinstance(text_samples, list):
            text_samples = []
        text_samples.append(float(latency))
        text_samples = text_samples[-100:]
        text_stats = compute_filtered_latency_stats(text_samples)

        record = {
            "updated_at": now_stamp(),
            "recent_first_pcm_latency_seconds": [round(sample, 4) for sample in first_pcm_samples],
            "raw_sample_count": first_pcm_stats["raw_count"],
            "filtered_sample_count": first_pcm_stats["filtered_count"],
            "average_first_pcm_latency_seconds": round(first_pcm_stats["average_seconds"], 4)
            if first_pcm_stats["average_seconds"] is not None
            else None,
            "median_first_pcm_latency_seconds": round(first_pcm_stats["median_seconds"], 4)
            if first_pcm_stats["median_seconds"] is not None
            else None,
            "recent_text_generation_completion_latency_seconds": [
                round(sample, 4) for sample in text_samples
            ],
            "text_generation_raw_sample_count": text_stats["raw_count"],
            "text_generation_filtered_sample_count": text_stats["filtered_count"],
            "average_text_generation_completion_latency_seconds": round(
                text_stats["average_seconds"], 4
            )
            if text_stats["average_seconds"] is not None
            else None,
            "median_text_generation_completion_latency_seconds": round(
                text_stats["median_seconds"], 4
            )
            if text_stats["median_seconds"] is not None
            else None,
            "outlier_strategy": "median_mad_then_trimmed_fallback",
        }
        write_pretty_json_file(TTS_FIRST_PCM_STATS_PATH, record)

    result["_text_generation_latency_recorded"] = True


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
                "Keep every sentence short and speakable. "
                "No labels. No JSON. No markdown."
            ),
            "interval_instruction": (
                "You are generating Counter-Strike 2 idle caster lines for live TTS. "
                "Return plain text only. "
                "Return exactly 3 short lines. "
                "Keep every sentence short and speakable. "
                "No labels. No JSON. No markdown."
            ),
            "event_system_prompt_template": (
                "{event_instruction} "
                "This is Counter-Strike 2. "
                "Line 1 should fit {caster0}'s rapid event-call style and be extremely short, ideally 2 to 5 words and never exceed 8 words. "
                "Line 2 should fit {caster1}'s short follow-up style and stay speakable. "
                "All sentences must stay short. Prefer a single short sentence per line. "
                "Treat tactical_facts as structured tactical metadata, not as wording to repeat. "
                "If analysis_mode is generic or position_data is none, rely on alive_counts, score_context, bomb_state, and recent events instead of lane-control claims. "
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
                "If analysis_mode is generic or position_data is none, rely on alive_counts, score_context, bomb_state, and recent events instead of lane-control claims. "
                "Use Tactical context to sound live rather than generic. "
                "Pick one or two signals that matter most instead of trying to explain everything. "
                "Do not quote enum labels verbatim; translate them into natural commentary. "
                "Vary phrasing rather than leaning on fixed wording. "
                "Prefer one concrete observation per line. Keep every sentence short. If confidence is low, stay tentative. "
                "Generate 3 understated {caster1} lines that avoid repeating the same point. "
                "Each line should be a single short sentence. "
                "Few-shot JSON examples:\n{few_shots_json}"
            ),
            "interval_system_prompt_conversation_template": (
                "{interval_instruction} "
                "This is Counter-Strike 2. "
                "Lean heavily into a Portal 2 style contrast: {caster0} should feel like a dry, clinical male announcer, and {caster1} should feel like an eager, slightly awkward turret personality. "
                "Be concise, grounded, and avoid repeating the same context. "
                "Treat tactical_facts as structured tactical metadata, not as ready-made commentary. "
                "If analysis_mode is generic or position_data is none, rely on alive_counts, score_context, bomb_state, and recent events instead of lane-control claims. "
                "Use Tactical context to sound live rather than generic. "
                "Do not quote enum labels verbatim; translate them into natural commentary. "
                "Keep every sentence short. "
                "Generate exactly 3 lines as a tiny conversation with this structure: line 1 is an idle comment, line 2 is a comment responding to the idle comment, line 3 is a response to that comment. "
                "The chemistry should come from the contrast between the voices, not from long jokes or rambling. "
                "Return commentary lines only with no speaker labels. "
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
        TTS_FIRST_PCM_STATS_PATH,
    ]:
        path.write_text("", encoding="utf-8")
    reset_log_clock()
    with QUEUE_CONDITION:
        items_to_cleanup = list(PLAYBACK_QUEUE)
        if CURRENT_PLAYBACK is not None:
            items_to_cleanup.append(CURRENT_PLAYBACK)
        PLAYBACK_QUEUE = deque()
        CURRENT_PLAYBACK = None
        QUEUE_WORKER_THREAD = None
        INTERVAL_MODE_INDEX = 0
        ITEM_SEQUENCE = 0
    for item in items_to_cleanup:
        finalize_item_prefetch(item, cancel=True, wait=False)


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
            "local_player": context.get("local_player"),
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
        "kill_summary": 98,
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


def event_followup_caster_from_wrapper(wrapper):
    request = as_dict(as_dict(as_dict(wrapper).get("input")).get("request"))
    request_lines = request.get("lines") or []
    if len(request_lines) > 1:
        caster = normalize_caster_id(as_dict(request_lines[1]).get("caster"))
        if caster in {CASTER0, CASTER1}:
            return caster
    return CASTER1


def interval_casters_from_wrapper(wrapper, *, conversation_mode=False):
    request = as_dict(as_dict(as_dict(wrapper).get("input")).get("request"))
    request_lines = request.get("lines") or []
    casters = []
    for request_line in request_lines:
        caster = normalize_caster_id(as_dict(request_line).get("caster"))
        if caster in {CASTER0, CASTER1}:
            casters.append(caster)
    if casters:
        return casters
    if conversation_mode:
        return [CASTER0, CASTER1, CASTER0]
    return [CASTER1, CASTER0, CASTER1]


def build_event_system_prompt(current_events, *, followup_caster=CASTER1):
    few_shots = select_few_shot_examples(
        casters={CASTER0, followup_caster},
        prompt_styles={"play_by_play_event", "play_by_play_follow_up"},
        current_events=current_events,
        limit=3,
    )
    config = load_prompt_config()
    prompt = render_prompt_template(
        config.get("event_system_prompt_template", ""),
        {
            "event_instruction": config.get("event_instruction", "").strip(),
            "caster0": CASTER0,
            "caster1": followup_caster,
            "few_shots_json": json.dumps(few_shots, indent=2, sort_keys=True),
        },
    )
    prompt = (
        f"{prompt} "
        "For Line 1, never say 'kill confirmed'."
    ).strip()
    return prompt


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
    followup_caster = event_followup_caster_from_wrapper(wrapper)
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
        f"Line 2: short {followup_caster} follow-up line that may use Tactical context.\n"
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
    caster_sequence = interval_casters_from_wrapper(wrapper, conversation_mode=conversation_mode)
    context = as_dict(wrapper_input.get("context"))
    local_player = as_dict(context.get("local_player"))
    limited_player_view = bool(local_player) and not context.get("alive_players")
    prompt_input = strip_empty(
        {
            "derived_tactical_summary": wrapper_input.get("derived_tactical_summary"),
            "request": wrapper_input.get("request"),
        }
    )
    mode_text = (
        f"Generate exactly 3 lines for a short {CASTER0}/{CASTER1} idle exchange."
        if conversation_mode
        else "Generate exactly 3 short idle analysis lines following the requested caster order."
    )
    requested_caster_order = ", ".join(caster_sequence)
    limited_view_note = (
        "Full team visibility is unavailable. Use local_player state, health, armor, money, active weapon, and carried utility as the primary live context.\n"
        if limited_player_view
        else ""
    )
    return (
        f"{mode_text}\n"
        "Use the Live context below.\n"
        "Use tactical_facts as facts to reason from, not text to copy.\n"
        f"{limited_view_note}"
        "Do not add labels or numbering.\n\n"
        f"Requested caster order: {requested_caster_order}\n\n"
        "Live context:\n"
        f"{json.dumps(build_idle_prompt_context(wrapper_input), indent=2, sort_keys=True)}\n\n"
        "Prompt input:\n"
        f"{json.dumps(prompt_input, indent=2, sort_keys=True)}"
    )


def strip_line_label_prefix(text):
    candidate = str(text or "")
    candidate = re.sub(r"(?i)^.*?\bline\s*[12]\s*:\s*", "", candidate).strip()
    return candidate


def is_retryable_blank_output(raw_text, lines=None):
    raw_text = str(raw_text or "").strip()
    if raw_text in {"[]", "[ ]", "{}"}:
        return True

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, list) and not parsed:
        return True
    if isinstance(parsed, dict) and isinstance(parsed.get("lines"), list) and not parsed.get("lines"):
        return True

    normalized_lines = [" ".join(str(line or "").split()).strip() for line in (lines or [])]
    return bool(normalized_lines) and all(line in {"[]", "{}"} for line in normalized_lines)


def extract_commentary_lines(raw_text, expected_max):
    raw_text = str(raw_text or "")

    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        structured_lines = parsed.get("lines")
        if isinstance(structured_lines, list):
            cleaned = []
            for line in structured_lines:
                candidate = " ".join(str(line or "").split()).strip().strip("`")
                candidate = strip_line_label_prefix(candidate)
                if not candidate:
                    continue
                cleaned.append(candidate)
                if len(cleaned) >= expected_max:
                    break
            if cleaned:
                return cleaned

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
        candidate = strip_line_label_prefix(candidate)
        if not candidate:
            continue
        if candidate.lower().startswith("json"):
            continue
        cleaned.append(candidate)
        if len(cleaned) >= expected_max:
            break

    if not cleaned:
        raise RuntimeError("text model returned no usable commentary lines")

    if is_retryable_blank_output(raw_text, cleaned):
        raise RuntimeError("text model returned blank array output")

    return cleaned


def request_commentary_lines_with_retry(
    text_config,
    system_prompt,
    user_prompt,
    *,
    expected_max,
    retry_attempts=1,
):
    last_error = None
    attempts = max(1, int(retry_attempts) + 1)
    for _ in range(attempts):
        result = request_chat_completion(text_config, system_prompt, user_prompt)
        record_text_generation_completion_latency(result)
        try:
            lines = extract_commentary_lines(result["raw_text"], expected_max=expected_max)
        except RuntimeError as error:
            last_error = error
            if is_retryable_blank_output(result.get("raw_text")):
                continue
            raise
        return result, lines

    raise last_error or RuntimeError("text model returned no usable commentary lines")


def split_compound_event_lines(lines, expected_max=None):
    split_lines = []
    for line in lines:
        sentences = re.split(r"(?<=[.!?])\s+", str(line).strip())
        for sentence in sentences:
            candidate = sentence.strip()
            if not candidate:
                continue
            split_lines.append(candidate)
            if expected_max is not None and len(split_lines) >= expected_max:
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


def _cleanup_prefetch_resources(item, *, wait=False):
    temp_dir_obj = item.get("prefetch_temp_dir_obj")
    thread = item.get("prefetch_thread")
    result = item.get("prefetch_result") or {}
    if temp_dir_obj is None:
        return

    if wait:
        if thread is not None:
            thread.join()
        record_tts_first_pcm_latency(result)
        temp_dir_obj.cleanup()
        item["prefetch_temp_dir_obj"] = None
        item["prefetch_cleanup_pending"] = False
        return

    if thread is None or not thread.is_alive():
        record_tts_first_pcm_latency(result)
        temp_dir_obj.cleanup()
        item["prefetch_temp_dir_obj"] = None
        item["prefetch_cleanup_pending"] = False
        return

    if item.get("prefetch_cleanup_pending"):
        return

    item["prefetch_cleanup_pending"] = True

    def cleanup_worker():
        thread.join()
        record_tts_first_pcm_latency(result)
        temp_dir_obj.cleanup()
        item["prefetch_temp_dir_obj"] = None
        item["prefetch_cleanup_pending"] = False

    threading.Thread(
        target=cleanup_worker,
        daemon=True,
        name=f"gsi-v5-prefetch-cleanup-{item.get('id')}",
    ).start()


def finalize_item_prefetch(item, *, cancel=False, wait=False):
    if cancel:
        cancel_event = item.get("prefetch_cancel_event")
        if cancel_event is not None:
            cancel_event.set()
    _cleanup_prefetch_resources(item, wait=wait)


def start_prefetch_for_item(item, repo_root, *, tts_config=None):
    with QUEUE_CONDITION:
        if item.get("prefetch_started"):
            return False

    if tts_config is None:
        tts_config = build_tts_config(repo_root)

    temp_dir_obj = tempfile.TemporaryDirectory(prefix="gsi_v5_tts_prefetch_")
    buffer_path = Path(temp_dir_obj.name) / f"item_{item['id']}.pcm"
    buffer_path.touch()
    cancel_event = threading.Event()
    result = {"done": False, "ok": False}
    tts_prompt = build_tts_prompt(
        item["commentary"],
        item["caster"],
        item["prompt_style"],
        tts_config,
    )
    thread = threading.Thread(
        target=fetch_tts_audio_to_file,
        args=(tts_config, tts_prompt, buffer_path, result, cancel_event),
        daemon=True,
        name=f"gsi-v5-prefetch-{item['id']}",
    )

    with QUEUE_CONDITION:
        if item.get("prefetch_started"):
            temp_dir_obj.cleanup()
            return False
        item["prefetch_started"] = True
        item["prefetch_buffer_path"] = buffer_path
        item["prefetch_cancel_event"] = cancel_event
        item["prefetch_result"] = result
        item["prefetch_thread"] = thread
        item["prefetch_temp_dir_obj"] = temp_dir_obj
        item["prefetch_cleanup_pending"] = False

    thread.start()
    return True


def ensure_head_prefetch(repo_root, *, tts_config=None):
    with QUEUE_CONDITION:
        candidate = PLAYBACK_QUEUE[0] if PLAYBACK_QUEUE else None
    if candidate is None:
        return False
    return start_prefetch_for_item(candidate, repo_root, tts_config=tts_config)


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

                ensure_head_prefetch(repo_root, tts_config=tts_config)

                cancel_prefetch = False
                try:
                    tts_prompt = build_tts_prompt(
                        CURRENT_PLAYBACK["commentary"],
                        CURRENT_PLAYBACK["caster"],
                        CURRENT_PLAYBACK["prompt_style"],
                        tts_config,
                    )
                    slim_log_tts_start(
                        CURRENT_PLAYBACK["id"],
                        tag=CURRENT_PLAYBACK["tag"],
                        caster=CURRENT_PLAYBACK["caster"],
                    )
                    if CURRENT_PLAYBACK.get("prefetch_started") and CURRENT_PLAYBACK.get("prefetch_buffer_path") is not None:
                        playback = stream_prefetched_tts_playback_interruptibly(
                            tts_config,
                            tts_prompt,
                            CURRENT_PLAYBACK["prefetch_buffer_path"],
                            CURRENT_PLAYBACK.get("prefetch_result") or {},
                            CURRENT_PLAYBACK["interrupt_event"],
                        )
                    else:
                        playback = play_tts_prompt_interruptibly(
                            tts_config,
                            tts_prompt,
                            CURRENT_PLAYBACK["interrupt_event"],
                        )
                except Exception as error:
                    cancel_prefetch = True
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
                    record_tts_first_pcm_latency(playback.get("fetch_result") or {})
                    if playback.get("interrupted"):
                        cancel_prefetch = True
                        slim_log_tts_finish(CURRENT_PLAYBACK["id"], "tts interrupted")
                    else:
                        slim_log_tts_finish(CURRENT_PLAYBACK["id"], "tts end")
                finally:
                    finalize_item_prefetch(CURRENT_PLAYBACK, cancel=cancel_prefetch, wait=True)
                    CURRENT_PLAYBACK["done_event"].set()
                    with QUEUE_CONDITION:
                        CURRENT_PLAYBACK = None
                        write_queue_state_locked()

        QUEUE_WORKER_THREAD = threading.Thread(target=worker, daemon=True, name="gsi-v5-tts-worker")
        QUEUE_WORKER_THREAD.start()


def enqueue_prompt_items(items, repo_root):
    ensure_queue_worker(repo_root)
    with QUEUE_CONDITION:
        for item in items:
            PLAYBACK_QUEUE.append(item)

        write_queue_state_locked()
        QUEUE_CONDITION.notify_all()

    ensure_head_prefetch(repo_root)

    return []


def normalize_team_key(team):
    team = str(normalize_team(team) or "").upper()
    if team == "CT":
        return "ct"
    if team == "T":
        return "t"
    return None


def empty_kill_counts():
    return {"ct": 0, "t": 0}


def add_kill_count(kill_counts, team, amount=1):
    team_key = normalize_team_key(team)
    if team_key is None:
        return
    try:
        amount_value = int(amount)
    except (TypeError, ValueError):
        amount_value = 0
    if amount_value <= 0:
        return
    kill_counts[team_key] = int(kill_counts.get(team_key, 0)) + amount_value


def classify_event_family(current_events):
    current_events = [as_dict(event) for event in current_events]
    event_types = {event.get("event_type") for event in current_events}
    if event_types & {"kill", "kill_summary", "kill_cluster", "player_scored_kill", "player_death"}:
        return "kill"
    if event_types & {"grenade_detonated", "grenade_thrown"}:
        return "grenade"
    return "other"


def collect_event_types(current_events):
    current_events = [as_dict(event) for event in current_events]
    return sorted(
        {
            str(event.get("event_type") or "").strip()
            for event in current_events
            if str(event.get("event_type") or "").strip()
        }
    )


def count_kills_by_team(current_events):
    current_events = [as_dict(event) for event in current_events]
    kill_counts = empty_kill_counts()

    for event in current_events:
        event_type = event.get("event_type")
        if event_type == "kill":
            add_kill_count(kill_counts, as_dict(event.get("killer")).get("team"))
        elif event_type == "kill_summary":
            add_kill_count(kill_counts, "CT", event.get("ct_kills", 0))
            add_kill_count(kill_counts, "T", event.get("t_kills", 0))
        elif event_type == "player_scored_kill":
            add_kill_count(kill_counts, as_dict(event.get("player")).get("team"))
        elif event_type == "kill_cluster":
            for killer in as_dict(event).get("killers", []) or []:
                add_kill_count(kill_counts, as_dict(killer).get("team"))

    return kill_counts


def total_kill_count(kill_counts):
    kill_counts = as_dict(kill_counts)
    return int(kill_counts.get("ct", 0)) + int(kill_counts.get("t", 0))


def prepare_queue_for_event_trigger(current_events=None):
    current_events = [as_dict(event) for event in (current_events or [])]
    incoming_kill_counts = count_kills_by_team(current_events)
    aggregated_event_types = set(collect_event_types(current_events))
    dropped_items = []
    interrupted_current = None
    aggregated_kill_counts = dict(incoming_kill_counts)

    with QUEUE_CONDITION:
        while PLAYBACK_QUEUE:
            tail_index = len(PLAYBACK_QUEUE) - 1
            tail_item = PLAYBACK_QUEUE[tail_index]
            tail_tag = tail_item.get("tag")

            if tail_tag not in {"idle", "followup"}:
                break

            if tail_tag == "followup":
                if tail_index == 0:
                    break
                previous_item = PLAYBACK_QUEUE[tail_index - 1]
                if previous_item.get("tag") == "event":
                    break

            dropped_items.append(PLAYBACK_QUEUE.pop())

        dropped_items.reverse()

        write_queue_state_locked()

    for item in dropped_items:
        finalize_item_prefetch(item, cancel=True, wait=False)

    return dropped_items, interrupted_current, aggregated_kill_counts, sorted(aggregated_event_types)


def build_queue_item(
    *,
    commentary,
    caster,
    prompt_style,
    tag,
    payload_sequence,
    source,
    event_family=None,
    kill_counts=None,
    event_types=None,
):
    item = {
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
        "prefetch_started": False,
        "prefetch_cleanup_pending": False,
    }
    if event_family is not None:
        item["event_family"] = event_family
    if kill_counts is not None:
        item["kill_counts"] = dict(kill_counts)
    if event_types is not None:
        item["event_types"] = list(event_types)
    return item


def is_spectator_mode(snapshot):
    snapshot = as_dict(snapshot)
    return bool(as_dict(snapshot.get("allplayers")))


def current_playback_is_grenade_event():
    with QUEUE_CONDITION:
        if CURRENT_PLAYBACK is None:
            return False
        return CURRENT_PLAYBACK.get("source") == "event" and CURRENT_PLAYBACK.get("event_family") == "grenade"


def should_ignore_event_prompt(wrapper, snapshot):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    current_events = [as_dict(event) for event in wrapper_input.get("current_events", [])]
    if not current_events:
        return False

    if all(event.get("event_type") == "grenade_thrown" for event in current_events) and is_spectator_mode(snapshot):
        return True

    if classify_event_family(current_events) == "grenade" and current_playback_is_grenade_event():
        return True

    return False


def process_event_wrapper(wrapper, repo_root, *, payload_sequence=None, snapshot=None):
    if should_ignore_event_prompt(wrapper, snapshot):
        return None

    current_events = as_dict(as_dict(wrapper).get("input")).get("current_events", [])
    followup_caster = event_followup_caster_from_wrapper(wrapper)
    event_family = classify_event_family(current_events)
    kill_counts = count_kills_by_team(current_events)
    event_types = collect_event_types(current_events)
    record = {
        "created_at": now_stamp(),
        "mode": "event",
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        result = None
        prompt_wrapper = copy.deepcopy(wrapper)
        queued_event_family = event_family
        queued_kill_counts = kill_counts
        queued_event_types = event_types
        text_config = build_v5_text_llm_config(repo_root)
        system_prompt = build_event_system_prompt(
            current_events,
            followup_caster=followup_caster,
        )
        user_prompt = build_event_user_prompt(prompt_wrapper)
        result, lines = request_commentary_lines_with_retry(
            text_config,
            system_prompt,
            user_prompt,
            expected_max=4,
        )
        lines = split_compound_event_lines(lines, expected_max=4)
        dropped = []
        interrupted_current = None
        if lines:
            dropped, interrupted_current, _, _ = prepare_queue_for_event_trigger(current_events)

        if lines and dropped:
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
                    event_family=queued_event_family,
                    kill_counts=queued_kill_counts,
                    event_types=queued_event_types,
                )
            )
        if len(lines) > 1:
            for followup_line in lines[1:]:
                items.append(
                    build_queue_item(
                        commentary=followup_line,
                        caster=followup_caster,
                        prompt_style="play_by_play_follow_up",
                        tag="followup",
                        payload_sequence=payload_sequence,
                        source="event",
                        event_family=queued_event_family,
                        kill_counts=queued_kill_counts,
                        event_types=queued_event_types,
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
        if result is not None:
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
    requested_casters = interval_casters_from_wrapper(wrapper, conversation_mode=conversation_mode)
    record = {
        "created_at": now_stamp(),
        "mode": interval_mode,
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        items = []
        text_config = build_v5_text_llm_config(repo_root)
        system_prompt = build_interval_system_prompt(conversation_mode)
        user_prompt = build_interval_user_prompt(wrapper, conversation_mode)
        result, lines = request_commentary_lines_with_retry(
            text_config,
            system_prompt,
            user_prompt,
            expected_max=3,
        )
        for index, line in enumerate(lines):
            caster = requested_casters[index] if index < len(requested_casters) else requested_casters[-1]
            sentence_lines = split_compound_event_lines([line])
            for sentence in sentence_lines:
                items.append(
                    build_queue_item(
                        commentary=sentence,
                        caster=caster,
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
