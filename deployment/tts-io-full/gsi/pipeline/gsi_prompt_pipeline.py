import copy
import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from text_llm_client import build_config as build_text_llm_config
from text_llm_client import request_plain_commentary
from text_llm_client import request_structured_commentary
from tts_client import build_config as build_tts_config
from tts_client import stream_tts_playback


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
OPENCAST_ROOT = SCRIPT_DIR.parents[3]
STATE_DIR = SCRIPT_DIR / ".state"
PROMPT_DB_PATH = STATE_DIR / "tts_prompt_database.json"
PIPELINE_LOG = STATE_DIR / "pipeline.log"


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


def env_bool(name, default=False):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


EXPECTED_TOKEN = env_text("CS2_GSI_AUTH_TOKEN", "") or None
HOST = env_text("CS2_GSI_HOST", "127.0.0.1")
PORT = env_int("CS2_GSI_PORT", 3000)
INTERVAL_SECONDS = env_float("CS2_GSI_PROMPT_INTERVAL", 2.0)
KILL_EXISTING_LISTENER = env_bool("CS2_GSI_KILL_EXISTING_LISTENER", True)
EVENT_DEBOUNCE_SECONDS = env_float("CS2_GSI_EVENT_DEBOUNCE", 0.5)
PROMPT_DB_MAX = env_int("CS2_GSI_PROMPT_DB_MAX", 50)
NEAR_END_SCORE = env_int("CS2_GSI_NEAR_END_SCORE", 11)
TEXT_LLM_EXPECT_JSON = env_bool("TEXT_LLM_EXPECT_JSON", True)
TEXT_LLM_CONFIG = build_text_llm_config(OPENCAST_ROOT)
TTS_CONFIG = build_tts_config(OPENCAST_ROOT)

STATE_LOCK = threading.Lock()
PROMPT_JOB_QUEUE = queue.Queue()
TTS_JOB_QUEUE = queue.Queue()


def now_epoch():
    return time.time()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def as_dict(value):
    return value if isinstance(value, dict) else {}


def compact_copy(value):
    if isinstance(value, dict):
        return {key: compact_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_copy(item) for item in value]
    return value


def append_log(text):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with PIPELINE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def split_path(path):
    return str(path).split(".")


def normalize_path(path):
    parts = split_path(path)
    return ".".join("*" if part.isdigit() else part for part in parts)


def extract_changed_paths(node, prefix=""):
    if not isinstance(node, dict):
        return [prefix] if prefix else []

    paths = []
    for key, value in node.items():
        next_prefix = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, dict):
            child_paths = extract_changed_paths(value, next_prefix)
            paths.extend(child_paths or [next_prefix])
        else:
            paths.append(next_prefix)
    return paths


def parse_vec3(text):
    if not isinstance(text, str):
        return None
    parts = [part.strip() for part in text.split(",")]
    if len(parts) != 3:
        return None
    try:
        return tuple(float(part) for part in parts)
    except ValueError:
        return None


def distance_between(a, b):
    if not a or not b:
        return None
    return math.sqrt(sum((a[index] - b[index]) ** 2 for index in range(3)))


def find_listening_pids(port):
    commands = [
        ["lsof", "-ti", f"TCP:{port}", "-sTCP:LISTEN"],
        ["fuser", f"{port}/tcp"],
    ]

    for command in commands:
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            continue

        text = (result.stdout or "").strip()
        if not text:
            continue

        pids = set()
        for token in text.replace("\n", " ").split():
            try:
                pids.add(int(token))
            except ValueError:
                continue
        if pids:
            return sorted(pids)

    return []


def pid_is_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def reclaim_port(port):
    current_pid = os.getpid()
    pids = [pid for pid in find_listening_pids(port) if pid != current_pid]
    if not pids:
        return

    print(f"Port {port} is busy; reclaiming it from PID(s): {', '.join(map(str, pids))}", flush=True)

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    deadline = time.time() + 2.0
    while time.time() < deadline:
        alive = [pid for pid in pids if pid_is_alive(pid)]
        if not alive:
            return
        time.sleep(0.1)

    for pid in pids:
        if not pid_is_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


class PromptDatabase:
    def __init__(self, path, max_items):
        self.path = path
        self.max_items = max_items
        self.records = deque(maxlen=max_items)
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = []
        for item in data[-self.max_items:]:
            self.records.append(item)

    def _write(self):
        self.path.write_text(
            json.dumps(list(self.records), indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def append(self, item):
        with self.lock:
            self.records.append(item)
            self._write()

    def size(self):
        return len(self.records)

    def update(self, record_id, patch):
        with self.lock:
            updated = False
            next_records = deque(maxlen=self.max_items)
            for item in self.records:
                if item.get("id") == record_id:
                    merged = copy.deepcopy(item)
                    merged.update(patch)
                    next_records.append(merged)
                    updated = True
                else:
                    next_records.append(item)
            self.records = next_records
            if updated:
                self._write()
            return updated


@dataclass
class PipelineState:
    latest_snapshot: dict | None = None
    previous_snapshot: dict | None = None
    payload_count: int = 0
    last_interval_prompt_at: float | None = None
    last_meaningful_event_at: float | None = None
    last_kill_at: float | None = None
    last_prompt_at: float | None = None
    last_round_number: int | None = None
    last_event_emit_at: float | None = None
    last_event_paths: list[str] = field(default_factory=list)
    last_event_details: list[dict] = field(default_factory=list)
    pending_event_paths: set[str] = field(default_factory=set)
    pending_event_reasons: set[str] = field(default_factory=set)
    prompt_worker_busy: bool = False
    tts_worker_busy: bool = False
    tts_backlog_count: int = 0


PROMPT_DB = PromptDatabase(PROMPT_DB_PATH, PROMPT_DB_MAX)
PIPELINE_STATE = PipelineState()


def compute_alive_counts(snapshot):
    counts = {"CT": 0, "T": 0}
    allplayers = as_dict(snapshot.get("allplayers"))

    if not allplayers:
        return counts

    for player in allplayers.values():
        player_dict = as_dict(player)
        team = player_dict.get("team")
        health = as_dict(player_dict.get("state")).get("health", 0)
        if team in counts and isinstance(health, (int, float)) and health > 0:
            counts[team] += 1

    return counts


def compute_max_round_kills(snapshot):
    max_round_kills = 0

    player_round_kills = as_dict(as_dict(snapshot.get("player")).get("state")).get("round_kills")
    if isinstance(player_round_kills, int):
        max_round_kills = max(max_round_kills, player_round_kills)

    for player in as_dict(snapshot.get("allplayers")).values():
        round_kills = as_dict(as_dict(player).get("state")).get("round_kills")
        if isinstance(round_kills, int):
            max_round_kills = max(max_round_kills, round_kills)

    return max_round_kills


def compute_score(snapshot):
    map_data = as_dict(snapshot.get("map"))
    ct = as_dict(map_data.get("team_ct")).get("score")
    t = as_dict(map_data.get("team_t")).get("score")
    return {
        "ct": ct if isinstance(ct, int) else 0,
        "t": t if isinstance(t, int) else 0,
    }


def compute_nearest_enemy_distance(snapshot):
    player = as_dict(snapshot.get("player"))
    focus_team = player.get("team")
    focus_position = parse_vec3(player.get("position"))
    if not focus_team or not focus_position:
        return None

    best = None
    for other in as_dict(snapshot.get("allplayers")).values():
        other_dict = as_dict(other)
        if other_dict.get("team") == focus_team:
            continue
        other_health = as_dict(other_dict.get("state")).get("health", 0)
        if not isinstance(other_health, (int, float)) or other_health <= 0:
            continue
        other_position = parse_vec3(other_dict.get("position"))
        distance = distance_between(focus_position, other_position)
        if distance is None:
            continue
        if best is None or distance < best:
            best = distance

    return round(best, 1) if best is not None else None


def collect_grenade_summary(snapshot):
    grenades = []
    for block_name in ["grenades", "allgrenades"]:
        for grenade_id, grenade in as_dict(snapshot.get(block_name)).items():
            grenade_dict = as_dict(grenade)
            grenades.append(
                {
                    "block": block_name,
                    "grenade_id": str(grenade_id),
                    "type": grenade_dict.get("type") or grenade_dict.get("weapon"),
                    "owner": grenade_dict.get("owner"),
                    "position": grenade_dict.get("position"),
                    "velocity": grenade_dict.get("velocity"),
                    "lifetime": grenade_dict.get("lifetime"),
                }
            )
    return grenades


def get_local_player_name(snapshot):
    return as_dict(snapshot.get("player")).get("name")


def build_player_index(snapshot):
    index = {}

    for entity_id, player in as_dict(snapshot.get("allplayers")).items():
        player_dict = as_dict(player)
        if player_dict:
            index[f"allplayers.{entity_id}"] = player_dict

    local_player = as_dict(snapshot.get("player"))
    if local_player:
        index["player"] = local_player

    return index


def get_player_name(player_dict, fallback):
    return player_dict.get("name") or fallback


def get_player_health(player_dict):
    health = as_dict(player_dict.get("state")).get("health")
    return health if isinstance(health, (int, float)) else None


def summarize_snapshot(snapshot):
    player = as_dict(snapshot.get("player"))
    player_state = as_dict(player.get("state"))
    map_data = as_dict(snapshot.get("map"))
    round_data = as_dict(snapshot.get("round"))
    bomb_data = as_dict(snapshot.get("bomb"))
    score = compute_score(snapshot)
    alive_counts = compute_alive_counts(snapshot)

    return {
        "map": map_data.get("name"),
        "mode": map_data.get("mode"),
        "map_phase": map_data.get("phase"),
        "round_number": map_data.get("round"),
        "round_phase": round_data.get("phase"),
        "round_win_team": round_data.get("win_team"),
        "bomb_state": bomb_data.get("state"),
        "score": score,
        "focus_player": {
            "name": player.get("name"),
            "team": player.get("team"),
            "activity": player.get("activity"),
            "health": player_state.get("health"),
            "armor": player_state.get("armor"),
            "money": player_state.get("money"),
            "position": player.get("position"),
        },
        "alive_counts": alive_counts,
        "allplayers_count": len(as_dict(snapshot.get("allplayers"))),
        "grenades_count": len(as_dict(snapshot.get("grenades"))),
        "allgrenades_count": len(as_dict(snapshot.get("allgrenades"))),
        "grenade_samples": collect_grenade_summary(snapshot)[:8],
        "nearest_enemy_distance": compute_nearest_enemy_distance(snapshot),
        "max_round_kills": compute_max_round_kills(snapshot),
    }


def is_noisy_numeric_path(path):
    normalized = normalize_path(path)
    parts = split_path(normalized)

    if any(part in {"position", "forward", "velocity"} for part in parts):
        return True

    if normalized.endswith("phase_countdowns.phase_ends_in"):
        return True

    if normalized.endswith(("ammo_clip", "ammo_clip_max", "ammo_reserve", "health", "armor", "money")):
        return True

    if normalized.endswith(("equip_value", "flashed", "smoked", "burning", "round_totaldmg")):
        return True

    if normalized.endswith("lifetime"):
        return True

    return False


def classify_event_paths(payload):
    raw_paths = extract_changed_paths(payload.get("added")) + extract_changed_paths(payload.get("previously"))
    normalized_paths = sorted(set(normalize_path(path) for path in raw_paths if path))

    meaningful = []
    reasons = set()

    for path in normalized_paths:
        if path.startswith(("grenades", "allgrenades")):
            meaningful.append(path)
            reasons.add("grenade_event")
            continue

        if path in {"map.phase", "map.round", "round.phase", "round.win_team", "round.bomb", "bomb.state"}:
            meaningful.append(path)
            reasons.add("round_or_bomb_event")
            continue

        if path in {"map.team_ct.score", "map.team_t.score"}:
            meaningful.append(path)
            reasons.add("score_event")
            continue

        if path in {"player.activity", "player.spectarget"}:
            meaningful.append(path)
            reasons.add("player_state_event")
            continue

        if path.endswith(("match_stats.kills", "state.round_kills", "state.round_killhs")):
            meaningful.append(path)
            reasons.add("kill_event")
            continue

        if ".weapons." in path and path.endswith((".name", ".type", ".state")):
            meaningful.append(path)
            reasons.add("weapon_event")
            continue

        if is_noisy_numeric_path(path):
            continue

    return meaningful, sorted(reasons)


def detect_phase_and_score_events(previous_snapshot, current_snapshot):
    events = []

    previous_map = as_dict(previous_snapshot.get("map"))
    current_map = as_dict(current_snapshot.get("map"))
    previous_round = as_dict(previous_snapshot.get("round"))
    current_round = as_dict(current_snapshot.get("round"))
    previous_bomb = as_dict(previous_snapshot.get("bomb"))
    current_bomb = as_dict(current_snapshot.get("bomb"))

    pairs = [
        ("map_phase", previous_map.get("phase"), current_map.get("phase"), "round_or_bomb_event"),
        ("round_phase", previous_round.get("phase"), current_round.get("phase"), "round_or_bomb_event"),
        ("bomb_state", previous_bomb.get("state"), current_bomb.get("state"), "round_or_bomb_event"),
        ("round_number", previous_map.get("round"), current_map.get("round"), "round_or_bomb_event"),
        (
            "ct_score",
            as_dict(previous_map.get("team_ct")).get("score"),
            as_dict(current_map.get("team_ct")).get("score"),
            "score_event",
        ),
        (
            "t_score",
            as_dict(previous_map.get("team_t")).get("score"),
            as_dict(current_map.get("team_t")).get("score"),
            "score_event",
        ),
    ]

    for label, previous_value, current_value, reason in pairs:
        if previous_value != current_value:
            events.append(
                {
                    "reason": reason,
                    "kind": label,
                    "previous": previous_value,
                    "current": current_value,
                }
            )

    return events


def detect_player_state_events(previous_snapshot, current_snapshot):
    events = []
    previous_players = build_player_index(previous_snapshot)
    current_players = build_player_index(current_snapshot)

    for player_key, current_player in current_players.items():
        previous_player = as_dict(previous_players.get(player_key))
        if not previous_player:
            continue

        previous_activity = previous_player.get("activity")
        current_activity = current_player.get("activity")
        if previous_activity != current_activity and current_activity is not None:
            events.append(
                {
                    "reason": "player_state_event",
                    "kind": "activity_changed",
                    "player": get_player_name(current_player, player_key),
                    "previous": previous_activity,
                    "current": current_activity,
                }
            )

        previous_spectarget = previous_player.get("spectarget")
        current_spectarget = current_player.get("spectarget")
        if previous_spectarget != current_spectarget and current_spectarget is not None:
            events.append(
                {
                    "reason": "player_state_event",
                    "kind": "spectarget_changed",
                    "player": get_player_name(current_player, player_key),
                    "previous": previous_spectarget,
                    "current": current_spectarget,
                }
            )

    return events


def detect_kill_events(previous_snapshot, current_snapshot):
    events = []
    previous_players = build_player_index(previous_snapshot)
    current_players = build_player_index(current_snapshot)

    for player_key, current_player in current_players.items():
        previous_player = as_dict(previous_players.get(player_key))
        if not previous_player:
            continue

        player_name = get_player_name(current_player, player_key)
        previous_state = as_dict(previous_player.get("state"))
        current_state = as_dict(current_player.get("state"))
        previous_match = as_dict(previous_player.get("match_stats"))
        current_match = as_dict(current_player.get("match_stats"))

        previous_health = get_player_health(previous_player)
        current_health = get_player_health(current_player)
        if (
            previous_health is not None
            and current_health is not None
            and previous_health > 0
            and current_health <= 0
        ):
            events.append(
                {
                    "reason": "kill_event",
                    "kind": "player_died",
                    "player": player_name,
                    "team": current_player.get("team"),
                }
            )

        previous_round_kills = previous_state.get("round_kills")
        current_round_kills = current_state.get("round_kills")
        if isinstance(previous_round_kills, int) and isinstance(current_round_kills, int):
            if current_round_kills > previous_round_kills:
                events.append(
                    {
                        "reason": "kill_event",
                        "kind": "round_kills_increased",
                        "player": player_name,
                        "previous": previous_round_kills,
                        "current": current_round_kills,
                    }
                )

        previous_total_kills = previous_match.get("kills")
        current_total_kills = current_match.get("kills")
        if isinstance(previous_total_kills, int) and isinstance(current_total_kills, int):
            if current_total_kills > previous_total_kills:
                events.append(
                    {
                        "reason": "kill_event",
                        "kind": "match_kills_increased",
                        "player": player_name,
                        "previous": previous_total_kills,
                        "current": current_total_kills,
                    }
                )

    return events


def detect_grenade_events(previous_snapshot, current_snapshot):
    events = []

    for block_name in ["grenades", "allgrenades"]:
        previous_block = as_dict(previous_snapshot.get(block_name))
        current_block = as_dict(current_snapshot.get(block_name))

        for grenade_id, current_grenade in current_block.items():
            if grenade_id in previous_block:
                continue
            grenade_dict = as_dict(current_grenade)
            events.append(
                {
                    "reason": "grenade_event",
                    "kind": "grenade_appeared",
                    "block": block_name,
                    "grenade_id": str(grenade_id),
                    "type": grenade_dict.get("type") or grenade_dict.get("weapon"),
                    "owner": grenade_dict.get("owner"),
                    "position": grenade_dict.get("position"),
                    "velocity": grenade_dict.get("velocity"),
                }
            )

    return events


def detect_weapon_events(previous_snapshot, current_snapshot):
    events = []
    previous_players = build_player_index(previous_snapshot)
    current_players = build_player_index(current_snapshot)

    local_name = get_local_player_name(current_snapshot)

    for player_key, current_player in current_players.items():
        previous_player = as_dict(previous_players.get(player_key))
        if not previous_player:
            continue

        player_name = get_player_name(current_player, player_key)
        if local_name and player_name != local_name:
            continue

        previous_weapons = as_dict(previous_player.get("weapons"))
        current_weapons = as_dict(current_player.get("weapons"))

        for weapon_slot, current_weapon in current_weapons.items():
            previous_weapon = as_dict(previous_weapons.get(weapon_slot))
            current_weapon_dict = as_dict(current_weapon)
            if not current_weapon_dict:
                continue

            for field_name in ["name", "type", "state"]:
                previous_value = previous_weapon.get(field_name)
                current_value = current_weapon_dict.get(field_name)
                if previous_value != current_value and current_value is not None:
                    events.append(
                        {
                            "reason": "weapon_event",
                            "kind": f"weapon_{field_name}_changed",
                            "player": player_name,
                            "weapon_slot": weapon_slot,
                            "previous": previous_value,
                            "current": current_value,
                        }
                    )

    return events


def dedupe_event_details(event_details):
    deduped = []
    seen = set()

    for event in event_details:
        key = stable_event_key(event)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(event)

    return deduped


def stable_event_key(event):
    return json.dumps(event, sort_keys=True, separators=(",", ":"))


def collect_semantic_events(previous_snapshot, current_snapshot, payload):
    fallback_paths, fallback_reasons = classify_event_paths(payload)

    if not previous_snapshot:
        return fallback_paths, fallback_reasons, []

    event_details = []
    event_details.extend(detect_phase_and_score_events(previous_snapshot, current_snapshot))
    event_details.extend(detect_player_state_events(previous_snapshot, current_snapshot))
    event_details.extend(detect_kill_events(previous_snapshot, current_snapshot))
    event_details.extend(detect_grenade_events(previous_snapshot, current_snapshot))
    event_details.extend(detect_weapon_events(previous_snapshot, current_snapshot))
    event_details = dedupe_event_details(event_details)

    reasons = sorted(set(fallback_reasons) | {event["reason"] for event in event_details})
    return fallback_paths, reasons, event_details


def build_instruction(caster_override, emotion_override, trigger_type, trigger_reasons):
    commentary_mode = "color commentary" if caster_override == "color" else "play-by-play commentary"
    joined_reasons = ", ".join(trigger_reasons) if trigger_reasons else "interval_update"
    return (
        "You are a live Counter-Strike commentator using a single consistent caster voice. "
        f"Speak as {commentary_mode}. "
        f"Current emotion should be {emotion_override.lower()}. "
        f"This prompt was triggered by {trigger_type}: {joined_reasons}. "
        "Prefer one concise spoken line, grounded in the provided gameplay snapshot."
    )


def is_live_gameplay(summary):
    return summary.get("map_phase") == "live"


def derive_overrides(snapshot, trigger_reasons, event_details):
    now = now_epoch()
    summary = summarize_snapshot(snapshot)
    alive_counts = summary["alive_counts"]
    total_alive = alive_counts["CT"] + alive_counts["T"]
    score = summary["score"]
    max_round_kills = summary["max_round_kills"]
    nearest_enemy_distance = summary["nearest_enemy_distance"]
    live_gameplay = is_live_gameplay(summary)
    has_kill_event = "kill_event" in trigger_reasons
    recent_kill = (
        PIPELINE_STATE.last_kill_at is not None
        and now - PIPELINE_STATE.last_kill_at < 3
    )

    no_recent_events = (
        PIPELINE_STATE.last_meaningful_event_at is not None
        and now - PIPELINE_STATE.last_meaningful_event_at >= 10
    )
    no_recent_kill = (
        PIPELINE_STATE.last_kill_at is None
        or now - PIPELINE_STATE.last_kill_at >= 3
    )
    near_end_of_game = live_gameplay and max(score["ct"], score["t"]) >= NEAR_END_SCORE
    killstreak_live = live_gameplay and max_round_kills >= 3

    if "new_round" in trigger_reasons:
        emotion = "Excited"
    elif killstreak_live or near_end_of_game:
        emotion = "Screaming"
    elif live_gameplay and no_recent_events and total_alive >= 8:
        emotion = "Calm"
    elif not live_gameplay and no_recent_events:
        emotion = "Calm"
    else:
        emotion = "Excited"

    caster_override = "color" if no_recent_kill else "play_by_play"

    if emotion == "Calm":
        speed = 0.90
    elif emotion == "Screaming":
        speed = 1.18
    elif nearest_enemy_distance is not None and nearest_enemy_distance < 400:
        speed = 1.12
    else:
        speed = 1.06

    summary["recent_kill"] = recent_kill
    summary["has_kill_event"] = has_kill_event
    summary["live_gameplay"] = live_gameplay
    summary["derived_event_count"] = len(event_details)

    return caster_override, emotion, speed, summary


def build_prompt_record(snapshot, trigger_type, trigger_reasons, changed_paths, event_details):
    caster_override, emotion_override, speed_override, summary = derive_overrides(
        snapshot,
        trigger_reasons,
        event_details,
    )
    instruction = build_instruction(caster_override, emotion_override, trigger_type, trigger_reasons)

    prompt_structured = {
        "instruction": instruction,
        "gameplay_snapshot": compact_copy(snapshot),
        "gameplay_summary": summary,
        "recent_event_details": event_details,
        "caster_override": caster_override,
        "emotion_override": emotion_override,
        "speed_override": speed_override,
        "output_schema": {
            "commentary": "string",
            "caster": "string",
            "emotion": "string",
        },
    }

    prompt_lines = [
        f"Instruction:\n{instruction}",
        "Gameplay snapshot:",
        json.dumps(snapshot, indent=2, sort_keys=True),
        "Gameplay summary:",
        json.dumps(summary, indent=2, sort_keys=True),
        "Recent event details:",
        json.dumps(event_details, indent=2, sort_keys=True),
        f"Caster override:\n{caster_override}",
        f"Emotion override:\n{emotion_override}",
        f"Speed override:\n{speed_override}",
    ]
    if TEXT_LLM_EXPECT_JSON:
        prompt_lines.append(
            "Return JSON:\n{\n  \"commentary\": \"...\",\n  \"caster\": \"...\",\n  \"emotion\": \"...\"\n}"
        )
    else:
        prompt_lines.append("Return only the commentary line as plain text.")
    prompt_text = "\n\n".join(prompt_lines)

    return {
        "id": str(uuid.uuid4()),
        "created_at": now_stamp(),
        "status": "queued_for_llm",
        "trigger": {
            "type": trigger_type,
            "reasons": trigger_reasons,
            "changed_paths": changed_paths,
            "event_details": event_details,
        },
        "prompt": prompt_structured,
        "prompt_text": prompt_text,
        "llm": {
            "status": "pending",
            "model_api_base": TEXT_LLM_CONFIG.model_api_base,
            "model_name": TEXT_LLM_CONFIG.model_name,
        },
        "tts_prompt": None,
    }


def should_skip_prompt_emit():
    currently_playing_count = 1 if PIPELINE_STATE.tts_worker_busy and PIPELINE_STATE.tts_backlog_count > 0 else 0
    queued_for_tts_count = max(0, PIPELINE_STATE.tts_backlog_count - currently_playing_count)
    llm_in_flight_count = 1 if PIPELINE_STATE.prompt_worker_busy else 0

    # Allow playback to have at most one "next up" item ahead of it.
    return queued_for_tts_count + llm_in_flight_count >= 1


def voice_name_for_emotion(emotion):
    normalized = (emotion or "").strip().lower()
    if normalized == "screaming":
        return "clone:scrawny_e2"
    if normalized == "excited":
        return "clone:scrawny_e1"
    return "clone:scrawny_e0"


def normalize_llm_result(record, llm_result):
    parsed = llm_result["parsed"] or {}
    prompt = record["prompt"]

    commentary = parsed.get("commentary") if isinstance(parsed, dict) else None
    if not isinstance(commentary, str) or not commentary.strip():
        commentary = llm_result["raw_text"].strip()

    caster = parsed.get("caster")
    if caster not in {"play_by_play", "color"}:
        caster = prompt["caster_override"]

    emotion = parsed.get("emotion")
    if emotion not in {"Calm", "Excited", "Screaming"}:
        emotion = prompt["emotion_override"]
    voice_name = voice_name_for_emotion(emotion)

    return {
        "commentary": commentary.strip(),
        "caster": caster,
        "emotion": emotion,
        "speed": prompt["speed_override"],
        "voice_name": voice_name,
    }


def prompt_worker():
    while True:
        record = PROMPT_JOB_QUEUE.get()
        record_id = record["id"]
        PIPELINE_STATE.prompt_worker_busy = True

        try:
            PROMPT_DB.update(
                record_id,
                {
                    "status": "llm_in_progress",
                    "started_at": now_stamp(),
                    "llm": {
                        "status": "in_progress",
                        "model_api_base": TEXT_LLM_CONFIG.model_api_base,
                        "model_name": TEXT_LLM_CONFIG.model_name,
                    },
                },
            )
            if TEXT_LLM_EXPECT_JSON:
                llm_result = request_structured_commentary(TEXT_LLM_CONFIG, record["prompt_text"])
            else:
                llm_result = request_plain_commentary(TEXT_LLM_CONFIG, record["prompt_text"])
            tts_prompt = normalize_llm_result(record, llm_result)
            PROMPT_DB.update(
                record_id,
                {
                    "status": "ready_for_tts",
                    "completed_at": now_stamp(),
                    "llm": {
                        "status": "completed",
                        "model_api_base": TEXT_LLM_CONFIG.model_api_base,
                        "model_name": TEXT_LLM_CONFIG.model_name,
                        "raw_text": llm_result["raw_text"],
                        "parsed": llm_result["parsed"],
                        "request": llm_result["request"],
                    },
                    "tts_status": "queued",
                    "tts_prompt": tts_prompt,
                },
            )
            PIPELINE_STATE.tts_backlog_count += 1
            TTS_JOB_QUEUE.put({"record_id": record_id, "tts_prompt": tts_prompt})
            print(
                f"[llm] completed record {record_id} voice={tts_prompt['voice_name']} commentary={tts_prompt['commentary']}",
                flush=True,
            )
            append_log(
                f"[{now_stamp()}] llm completed record {record_id} "
                f"voice={tts_prompt['voice_name']} caster={tts_prompt['caster']} "
                f"emotion={tts_prompt['emotion']}\n"
            )
        except Exception as error:
            PROMPT_DB.update(
                record_id,
                {
                    "status": "llm_failed",
                    "completed_at": now_stamp(),
                    "llm": {
                        "status": "failed",
                        "model_api_base": TEXT_LLM_CONFIG.model_api_base,
                        "model_name": TEXT_LLM_CONFIG.model_name,
                        "error": str(error),
                    },
                },
            )
            print(f"[llm] failed record {record_id}: {error}", flush=True)
            append_log(f"[{now_stamp()}] llm failed record {record_id}: {error}\n")
        finally:
            PIPELINE_STATE.prompt_worker_busy = False
            PROMPT_JOB_QUEUE.task_done()


def tts_worker():
    while True:
        job = TTS_JOB_QUEUE.get()
        record_id = job["record_id"]
        tts_prompt = job["tts_prompt"]
        PIPELINE_STATE.tts_worker_busy = True

        try:
            PROMPT_DB.update(
                record_id,
                {
                    "status": "tts_in_progress",
                    "tts_status": "in_progress",
                    "tts_started_at": now_stamp(),
                },
            )
            stream_tts_playback(TTS_CONFIG, tts_prompt)
            PROMPT_DB.update(
                record_id,
                {
                    "status": "tts_completed",
                    "tts_status": "completed",
                    "tts_completed_at": now_stamp(),
                },
            )
            print(f"[tts] completed record {record_id}", flush=True)
            append_log(f"[{now_stamp()}] tts completed record {record_id}\n")
        except Exception as error:
            PROMPT_DB.update(
                record_id,
                {
                    "status": "tts_failed",
                    "tts_status": "failed",
                    "tts_completed_at": now_stamp(),
                    "tts_error": str(error),
                },
            )
            print(f"[tts] failed record {record_id}: {error}", flush=True)
            append_log(f"[{now_stamp()}] tts failed record {record_id}: {error}\n")
        finally:
            PIPELINE_STATE.tts_worker_busy = False
            PIPELINE_STATE.tts_backlog_count = max(0, PIPELINE_STATE.tts_backlog_count - 1)
            TTS_JOB_QUEUE.task_done()


def emit_prompt(snapshot, trigger_type, trigger_reasons, changed_paths, event_details):
    if not snapshot:
        return

    if should_skip_prompt_emit():
        currently_playing_count = 1 if PIPELINE_STATE.tts_worker_busy and PIPELINE_STATE.tts_backlog_count > 0 else 0
        queued_for_tts_count = max(0, PIPELINE_STATE.tts_backlog_count - currently_playing_count)
        llm_in_flight_count = 1 if PIPELINE_STATE.prompt_worker_busy else 0
        print("[skip] prompt emit skipped because the next TTS slot is already reserved", flush=True)
        append_log(
            f"[{now_stamp()}] skipped prompt emit because queued_for_tts={queued_for_tts_count} "
            f"llm_in_flight={llm_in_flight_count} tts_backlog={PIPELINE_STATE.tts_backlog_count}\n"
        )
        return

    record = build_prompt_record(snapshot, trigger_type, trigger_reasons, changed_paths, event_details)
    PROMPT_DB.append(record)
    PROMPT_JOB_QUEUE.put(record)

    PIPELINE_STATE.last_prompt_at = now_epoch()
    PIPELINE_STATE.pending_event_paths.clear()
    PIPELINE_STATE.pending_event_reasons.clear()

    print(
        f"[prompt] {record['created_at']} stored {trigger_type} prompt "
        f"reasons={','.join(trigger_reasons) or 'interval_update'} "
        f"emotion={record['prompt']['emotion_override']} "
        f"caster={record['prompt']['caster_override']} "
        f"db_size={PROMPT_DB.size()}",
        flush=True,
    )
    append_log(
        f"[{record['created_at']}] stored prompt type={trigger_type} "
        f"reasons={','.join(trigger_reasons) or 'interval_update'} "
        f"emotion={record['prompt']['emotion_override']} "
        f"caster={record['prompt']['caster_override']} "
        f"map_phase={record['prompt']['gameplay_summary'].get('map_phase')} "
        f"round_phase={record['prompt']['gameplay_summary'].get('round_phase')} "
        f"max_round_kills={record['prompt']['gameplay_summary'].get('max_round_kills')} "
        f"db_size={PROMPT_DB.size()}\n"
    )


def interval_worker():
    while True:
        time.sleep(INTERVAL_SECONDS)

        with STATE_LOCK:
            snapshot = copy.deepcopy(PIPELINE_STATE.latest_snapshot)

        if not snapshot:
            continue

        emit_prompt(snapshot, "interval", ["interval_update"], [], [])
        PIPELINE_STATE.last_interval_prompt_at = now_epoch()


def maybe_handle_event_emit(previous_snapshot, current_snapshot, payload):
    meaningful_paths, reasons, event_details = collect_semantic_events(previous_snapshot, current_snapshot, payload)
    if not meaningful_paths and not event_details:
        return

    now = now_epoch()
    if (
        PIPELINE_STATE.last_event_emit_at is not None
        and now - PIPELINE_STATE.last_event_emit_at < EVENT_DEBOUNCE_SECONDS
    ):
        return

    snapshot = copy.deepcopy(current_snapshot)
    event_reasons = set(reasons)

    round_number = as_dict(snapshot.get("map")).get("round")
    if isinstance(round_number, int) and PIPELINE_STATE.last_round_number != round_number:
        if PIPELINE_STATE.last_round_number is not None:
            event_reasons.add("new_round")
        PIPELINE_STATE.last_round_number = round_number

    if "kill_event" in event_reasons:
        PIPELINE_STATE.last_kill_at = now

    PIPELINE_STATE.last_meaningful_event_at = now
    PIPELINE_STATE.last_event_emit_at = now
    PIPELINE_STATE.last_event_paths = meaningful_paths
    PIPELINE_STATE.last_event_details = event_details
    PIPELINE_STATE.pending_event_paths.update(meaningful_paths)
    PIPELINE_STATE.pending_event_reasons.update(event_reasons)

    emit_prompt(snapshot, "event", sorted(event_reasons), meaningful_paths, event_details)


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)

        try:
            payload = json.loads(body.decode("utf-8"))
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"Invalid JSON")
            return

        if EXPECTED_TOKEN is not None:
            token = as_dict(payload.get("auth")).get("token")
            if token != EXPECTED_TOKEN:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"Invalid auth token")
                return

        with STATE_LOCK:
            PIPELINE_STATE.previous_snapshot = copy.deepcopy(PIPELINE_STATE.latest_snapshot)
            PIPELINE_STATE.latest_snapshot = copy.deepcopy(payload)
            PIPELINE_STATE.payload_count += 1
            previous_snapshot = copy.deepcopy(PIPELINE_STATE.previous_snapshot)
            current_snapshot = copy.deepcopy(PIPELINE_STATE.latest_snapshot)

        maybe_handle_event_emit(previous_snapshot, current_snapshot, payload)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def reset_state():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_LOG.write_text("", encoding="utf-8")
    PIPELINE_STATE.latest_snapshot = None
    PIPELINE_STATE.previous_snapshot = None
    PIPELINE_STATE.payload_count = 0
    PIPELINE_STATE.last_interval_prompt_at = None
    PIPELINE_STATE.last_meaningful_event_at = None
    PIPELINE_STATE.last_kill_at = None
    PIPELINE_STATE.last_prompt_at = None
    PIPELINE_STATE.last_round_number = None
    PIPELINE_STATE.last_event_emit_at = None
    PIPELINE_STATE.last_event_paths.clear()
    PIPELINE_STATE.last_event_details.clear()
    PIPELINE_STATE.pending_event_paths.clear()
    PIPELINE_STATE.pending_event_reasons.clear()
    PIPELINE_STATE.prompt_worker_busy = False
    PIPELINE_STATE.tts_worker_busy = False
    PIPELINE_STATE.tts_backlog_count = 0
    append_log(f"[{now_stamp()}] pipeline reset\n")


def main():
    reset_state()

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    prompt_thread = threading.Thread(target=prompt_worker, daemon=True)
    prompt_thread.start()

    tts_thread = threading.Thread(target=tts_worker, daemon=True)
    tts_thread.start()

    thread = threading.Thread(target=interval_worker, daemon=True)
    thread.start()

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Prompt interval: {INTERVAL_SECONDS:.1f}s", flush=True)
    print(f"Prompt database: {PROMPT_DB_PATH}", flush=True)
    print(f"Pipeline log:    {PIPELINE_LOG}", flush=True)
    print(f"Text model:      {TEXT_LLM_CONFIG.model_name}", flush=True)
    print(f"LLM JSON mode:   {'on' if TEXT_LLM_EXPECT_JSON else 'off'}", flush=True)
    print(f"TTS endpoint:    {TTS_CONFIG.api_base}/v1/audio/speech", flush=True)
    print(f"TTS voice:       {TTS_CONFIG.voice_name}", flush=True)
    print(
        "This pipeline keeps one latest snapshot, emits prompts on interval + meaningful events, "
        "sends them to text-llm, then queues TTS playback sequentially.",
        flush=True,
    )

    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
