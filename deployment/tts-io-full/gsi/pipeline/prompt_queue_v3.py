import copy
import json
import os
import re
import tempfile
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
    fetch_tts_audio_to_file,
    open_play_process,
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
STATE_DIR = SCRIPT_DIR / ".state" / "v3"
PROMPT_RUNTIME_HISTORY_PATH = STATE_DIR / "prompt_runtime_pretty.jsonl"
PROMPT_RUNTIME_LATEST_PATH = STATE_DIR / "prompt_runtime_latest.json"
PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"
FEW_SHOT_EXAMPLES_PATH = SCRIPT_DIR / "few_shot_examples.json"
PROMPT_CONFIG_PATH = SCRIPT_DIR / "prompt_config_v3.json"

QUEUE_LOCK = threading.Lock()
QUEUE_CONDITION = threading.Condition(QUEUE_LOCK)
PLAYBACK_QUEUE = deque()
CURRENT_PLAYBACK = None
QUEUE_WORKER_THREAD = None
INTERVAL_MODE_INDEX = 0
ITEM_SEQUENCE = 0


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
                "Line 1 is the event trigger call. "
                "Line 2 is a short follow-up color line. "
                "No labels. No JSON. No markdown."
            ),
            "interval_instruction": (
                "You are generating Counter-Strike 2 idle caster lines for live TTS. "
                "Return plain text only. "
                "Return exactly 3 short lines. "
                "No labels. No JSON. No markdown."
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


def reset_prompt_runtime_state():
    global PLAYBACK_QUEUE, CURRENT_PLAYBACK, QUEUE_WORKER_THREAD, INTERVAL_MODE_INDEX, ITEM_SEQUENCE
    ensure_state_dir()
    for path in [
        PROMPT_RUNTIME_HISTORY_PATH,
        PROMPT_RUNTIME_LATEST_PATH,
        PROMPT_QUEUE_STATE_PATH,
    ]:
        path.write_text("", encoding="utf-8")
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
    example = as_dict(example)
    return strip_empty(
        {
            "input": {
                "match_context": {
                    "map_name": as_dict(example.get("input")).get("match_context", {}).get("map_name"),
                    "score": as_dict(example.get("input")).get("match_context", {}).get("score"),
                    "alive_players": as_dict(example.get("input")).get("match_context", {}).get("alive_players"),
                },
                "previous_events": as_dict(example.get("input")).get("previous_events"),
                "current_events": as_dict(example.get("input")).get("current_events"),
                "overrides": as_dict(example.get("input")).get("overrides"),
            },
            "output": {
                "commentary": as_dict(example.get("output")).get("commentary"),
                "prompt_style": as_dict(example.get("output")).get("prompt_style"),
                "caster": as_dict(example.get("output")).get("caster"),
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
    selected = []
    for example in load_few_shot_examples():
        output = as_dict(example.get("output"))
        if output.get("caster") not in casters:
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


def build_global_context(match_context):
    match_context = as_dict(match_context)
    return strip_empty(
        {
            "score": match_context.get("score"),
            "alive_players": match_context.get("alive_players"),
        }
    )


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
        casters={"play_by_play", "color"},
        prompt_styles={"play_by_play_event", "play_by_play_follow_up"},
        current_events=current_events,
        limit=5,
    )
    config = load_prompt_config()
    return (
        f"{config.get('event_instruction', '').strip()} "
        "This is Counter-Strike 2. "
        "Line 1 should be extremely short, ideally 2 to 5 words and never exceed 8 words. "
        "Line 2 should stay short and speakable. "
        "If the event is a kill and the killer has round_kills of 2 or more, prefer double, triple, quad, or ace style phrasing when appropriate. "
        "If the event is grenade_detonated, almost always mention detonation_callout. "
        "Few-shot JSON examples:\n"
        + json.dumps(few_shots, indent=2, sort_keys=True)
    ).strip()


def build_interval_system_prompt(conversation_mode):
    few_shots = select_few_shot_examples(
        casters={"color", "play_by_play"},
        prompt_styles={"idle_color"},
        limit=4,
    )
    config = load_prompt_config()
    extra = (
        "Generate a tiny 3-line conversation between play_by_play and color casters. "
        "The lines should sound like a short back-and-forth, but return commentary lines only with no speaker labels."
        if conversation_mode
        else
        "Generate 3 understated color lines that avoid repeating the same point."
    )
    return (
        f"{config.get('interval_instruction', '').strip()} "
        "This is Counter-Strike 2. "
        "Be concise, creative, and avoid repeating the same context. "
        f"{extra} "
        "Each line should be a single short sentence. "
        "Few-shot JSON examples:\n"
        + json.dumps(few_shots, indent=2, sort_keys=True)
    ).strip()


def build_event_user_prompt(wrapper):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "current_events": wrapper_input.get("current_events"),
            "overrides": wrapper_input.get("overrides"),
        }
    )
    return (
        "Generate exactly 2 lines.\n"
        "Line 1: very short event trigger call.\n"
        "Line 2: short follow-up color line.\n"
        "Use Focused context only for line 1.\n"
        "Do not add labels or numbering.\n\n"
        "Focused context:\n"
        f"{json.dumps(build_focused_context(wrapper_input.get('current_events', [])), indent=2, sort_keys=True)}\n\n"
        "Event input:\n"
        f"{json.dumps(prompt_input, indent=2, sort_keys=True)}"
    )


def build_interval_user_prompt(wrapper, conversation_mode):
    wrapper_input = as_dict(as_dict(wrapper).get("input"))
    prompt_input = strip_empty(
        {
            "overrides": wrapper_input.get("overrides"),
        }
    )
    mode_text = (
        "Generate exactly 3 lines for a short two-caster idle exchange."
        if conversation_mode
        else "Generate exactly 3 short idle color lines."
    )
    return (
        f"{mode_text}\n"
        "Use the Global context below.\n"
        "Do not add labels or numbering.\n\n"
        "Global context:\n"
        f"{json.dumps(build_global_context(wrapper_input.get('match_context')), indent=2, sort_keys=True)}\n\n"
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


def build_tts_prompt(commentary_text, caster, prompt_style, tts_config):
    voice_name = tts_config.voice_name
    speed = PLAY_BY_PLAY_SPEED if caster == "play_by_play" else COLOR_SPEED
    if caster == "play_by_play" and PLAY_BY_PLAY_VOICE_NAME:
        voice_name = PLAY_BY_PLAY_VOICE_NAME
    if caster == "color" and COLOR_VOICE_NAME:
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
    player = None
    thread = None
    temp_dir_obj = tempfile.TemporaryDirectory(prefix="gsi_tts_v3_")
    result = {"done": False, "ok": False}
    interrupted = False

    def close_player_immediately():
        nonlocal player
        if player is None:
            return
        if player.stdin is not None:
            try:
                player.stdin.close()
            except Exception:
                pass
            player.stdin = None
        if player.poll() is None:
            try:
                player.kill()
            except Exception:
                pass
            try:
                player.wait(timeout=0.5)
            except Exception:
                pass

    def finish_fetch_and_cleanup(fetch_thread, temp_dir):
        try:
            if fetch_thread is not None:
                fetch_thread.join()
        finally:
            temp_dir.cleanup()

    try:
        buffer_path = Path(temp_dir_obj.name) / "audio.pcm"
        buffer_path.touch()
        thread = threading.Thread(
            target=fetch_tts_audio_to_file,
            args=(tts_config, tts_prompt, buffer_path, result, interrupt_event),
            daemon=True,
        )
        thread.start()

        player = open_play_process(tts_config.sample_rate, speed=float(tts_prompt.get("speed") or 1.0))
        if player.stdin is None:
            raise RuntimeError("failed to open stdin for SoX play")

        offset = 0
        while True:
            if interrupt_event.is_set():
                interrupted = True
                return {"interrupted": True}

            size = buffer_path.stat().st_size if buffer_path.exists() else 0
            if size > offset:
                with buffer_path.open("rb") as handle:
                    handle.seek(offset)
                    while True:
                        if interrupt_event.is_set():
                            interrupted = True
                            return {"interrupted": True}
                        chunk = handle.read(min(16384, size - offset))
                        if not chunk:
                            break
                        player.stdin.write(chunk)
                        player.stdin.flush()
                        offset += len(chunk)
                continue

            if result.get("done"):
                if not result.get("ok"):
                    raise RuntimeError(result.get("error") or "TTS request failed")
                break

            time.sleep(0.01)

        player.stdin.close()
        player.stdin = None
        return_code = player.wait()
        if return_code != 0:
            raise RuntimeError(f"SoX play exited with status {return_code}")
        return {"interrupted": False}
    finally:
        close_player_immediately()
        if interrupted:
            cleanup_thread = threading.Thread(
                target=finish_fetch_and_cleanup,
                args=(thread, temp_dir_obj),
                daemon=True,
                name="gsi-v3-tts-cleanup",
            )
            cleanup_thread.start()
        else:
            finish_fetch_and_cleanup(thread, temp_dir_obj)


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
                else:
                    CURRENT_PLAYBACK["playback_result"] = playback
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
        "caster": caster,
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
        dropped = []
        interrupted_current = None
        if lines:
            dropped, interrupted_current = prepare_queue_for_event_trigger()
        items = []
        if lines:
            items.append(
                build_queue_item(
                    commentary=lines[0],
                    caster="play_by_play",
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
                        caster="color",
                        prompt_style="play_by_play_follow_up",
                        tag="followup",
                        payload_sequence=payload_sequence,
                        source="event",
                    )
                )

        enqueue_prompt_items(items, repo_root) if items else None
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


def process_interval_wrapper(wrapper, repo_root, *, payload_sequence=None):
    text_config = build_text_llm_config(repo_root)
    interval_mode = next_interval_mode()
    conversation_mode = interval_mode == "idle_conversation"
    system_prompt = build_interval_system_prompt(conversation_mode)
    user_prompt = build_interval_user_prompt(wrapper, conversation_mode)
    record = {
        "created_at": now_stamp(),
        "mode": interval_mode,
        "payload_sequence": payload_sequence,
        "status": "started",
        "prompt_input": copy.deepcopy(as_dict(wrapper).get("input")),
    }

    try:
        result = request_chat_completion(text_config, system_prompt, user_prompt)
        lines = extract_commentary_lines(result["raw_text"], expected_max=3)
        items = []
        if conversation_mode:
            casters = ["play_by_play", "color", "play_by_play"]
            for index, line in enumerate(lines):
                items.append(
                    build_queue_item(
                        commentary=line,
                        caster=casters[min(index, len(casters) - 1)],
                        prompt_style="idle_color",
                        tag="color",
                        payload_sequence=payload_sequence,
                        source=interval_mode,
                    )
                )
        else:
            commentary = join_commentary_lines(lines)
            if commentary:
                items.append(
                    build_queue_item(
                        commentary=commentary,
                        caster="color",
                        prompt_style="idle_color",
                        tag="color",
                        payload_sequence=payload_sequence,
                        source=interval_mode,
                    )
                )

        dropped = enqueue_prompt_items(items, repo_root) if items else []
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
