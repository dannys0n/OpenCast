import copy
import math
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from prompt_queue_v2 import (
    PROMPT_RUNTIME_HISTORY_PATH,
    PROMPT_RUNTIME_LATEST_PATH,
    process_filtered_batch,
    reset_prompt_runtime_state,
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
REPO_ROOT = SCRIPT_DIR.parents[3]
STATE_DIR = SCRIPT_DIR / ".state" / "v2"
MAP_CALLOUTS_DIR = SCRIPT_DIR / "map_callouts"
RAW_GSI_PATH = STATE_DIR / "gsi_received_pretty.jsonl"
RAW_GSI_LATEST_PATH = STATE_DIR / "gsi_received_latest.json"
FILTERED_EVENTS_PATH = STATE_DIR / "gsi_filtered_pretty.jsonl"
FILTERED_EVENTS_LATEST_PATH = STATE_DIR / "gsi_filtered_latest.json"
PIPELINE_LOG = STATE_DIR / "pipeline_v2.log"
MAP_CALLOUT_LINE_RE = re.compile(
    r'^\s*"(?P<name>[^"]+)"\s*,?\s*(?P<x>-?\d+(?:\.\d+)?)\s*,\s*(?P<y>-?\d+(?:\.\d+)?)\s*,\s*(?P<z>-?\d+(?:\.\d+)?)\s*$'
)


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


def env_bool(name, default=False):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


EXPECTED_TOKEN = env_text("CS2_GSI_AUTH_TOKEN", "") or None
HOST = env_text("CS2_GSI_HOST", "127.0.0.1")
PORT = env_int("CS2_GSI_PORT", 3000)
KILL_EXISTING_LISTENER = env_bool("CS2_GSI_KILL_EXISTING_LISTENER", True)

TEAM_NAMES = ("CT", "T")
ROUND_END_PHASES = {"over", "freezetime", "gameover", "intermission"}
IMPORTANT_DELTA_EXACT_PATHS = {
    "round.phase",
    "round.bomb",
    "round.win_team",
    "bomb.state",
    "map.team_ct.score",
    "map.team_t.score",
}
IMPORTANT_DELTA_SUFFIXES = (
    "state.round_kills",
    "match_stats.kills",
    "match_stats.deaths",
)
GRENADE_INVENTORY_DELTA_SUFFIXES = (
    "player.weapons.weapon_*.name",
    "player.weapons.weapon_*.state",
    "player.weapons.weapon_*.type",
    "player.weapons.weapon_*.ammo_reserve",
)
GRENADE_DELTA_ALLOWED_SUFFIXES = (
    ".owner",
    ".type",
    ".weapon",
    ".position",
    ".velocity",
    ".lifetime",
    ".effecttime",
)

STATE_LOCK = threading.Lock()


@dataclass
class PipelineState:
    latest_snapshot: dict | None = None
    previous_snapshot: dict | None = None
    payload_count: int = 0


PIPELINE_STATE = PipelineState()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def as_dict(value):
    return value if isinstance(value, dict) else {}


def normalize_team(value):
    if value in TEAM_NAMES:
        return value
    return value


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        RAW_GSI_PATH,
        RAW_GSI_LATEST_PATH,
        FILTERED_EVENTS_PATH,
        FILTERED_EVENTS_LATEST_PATH,
        PIPELINE_LOG,
    ]:
        path.touch(exist_ok=True)


def append_log(text):
    ensure_state_dir()
    with PIPELINE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def run_prompt_runtime_async(filtered_batch, payload_sequence=None):
    def worker():
        prompt_result = process_filtered_batch(filtered_batch, REPO_ROOT, payload_sequence=payload_sequence)
        prompt_status = prompt_result.get("status") if isinstance(prompt_result, dict) else None
        if prompt_status:
            print(f"[gsi] #{payload_sequence} prompt {prompt_status}", flush=True)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()


def append_pretty_json_record(path, record):
    ensure_state_dir()
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, indent=2, sort_keys=True))
        handle.write("\n\n")
        handle.flush()


def write_pretty_json_file(path, record):
    ensure_state_dir()
    path.write_text(f"{json.dumps(record, indent=2, sort_keys=True)}\n", encoding="utf-8")


def reset_session_files():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        RAW_GSI_PATH,
        RAW_GSI_LATEST_PATH,
        FILTERED_EVENTS_PATH,
        FILTERED_EVENTS_LATEST_PATH,
        PIPELINE_LOG,
    ]:
        path.write_text("", encoding="utf-8")


def split_path(path):
    return str(path).split(".")


def normalize_path(path):
    parts = split_path(path)
    normalized_parts = []
    for part in parts:
        if part.isdigit():
            normalized_parts.append("*")
            continue
        if part.startswith("weapon_") and part[len("weapon_"):].isdigit():
            normalized_parts.append("weapon_*")
            continue
        normalized_parts.append(part)
    return ".".join(normalized_parts)


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

    deadline = datetime.now().timestamp() + 2.0
    while datetime.now().timestamp() < deadline:
        alive = [pid for pid in pids if pid_is_alive(pid)]
        if not alive:
            return

    for pid in pids:
        if not pid_is_alive(pid):
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def compute_alive_counts(snapshot):
    counts = {"CT": 0, "T": 0}
    for player in as_dict(snapshot.get("allplayers")).values():
        player_dict = as_dict(player)
        team = player_dict.get("team")
        health = as_dict(player_dict.get("state")).get("health")
        if team in counts and isinstance(health, (int, float)) and health > 0:
            counts[team] += 1
    return counts


def compute_score(snapshot):
    map_data = as_dict(snapshot.get("map"))
    return {
        "ct": as_dict(map_data.get("team_ct")).get("score", 0)
        if isinstance(as_dict(map_data.get("team_ct")).get("score"), int)
        else 0,
        "t": as_dict(map_data.get("team_t")).get("score", 0)
        if isinstance(as_dict(map_data.get("team_t")).get("score"), int)
        else 0,
    }


def parse_position_vector(position_value):
    if isinstance(position_value, str):
        parts = [part.strip() for part in position_value.split(",")]
    elif isinstance(position_value, (list, tuple)):
        parts = list(position_value)
    else:
        return None

    if len(parts) < 3:
        return None

    try:
        return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (TypeError, ValueError):
        return None


@lru_cache(maxsize=None)
def load_map_callouts(map_name):
    if not map_name:
        return ()

    map_path = MAP_CALLOUTS_DIR / f"{map_name}.txt"
    if not map_path.exists():
        return ()

    callouts = []
    for raw_line in map_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue

        match = MAP_CALLOUT_LINE_RE.match(line)
        if not match:
            continue

        callouts.append(
            {
                "name": match.group("name"),
                "position": (
                    float(match.group("x")),
                    float(match.group("y")),
                    float(match.group("z")),
                ),
            }
        )

    return tuple(callouts)


def resolve_map_callout(map_name, position_value):
    position = parse_position_vector(position_value)
    if not position:
        return None

    callouts = load_map_callouts(map_name)
    if not callouts:
        return None

    closest_callout = None
    closest_distance = None
    for callout in callouts:
        callout_position = callout["position"]
        distance = math.dist(position, callout_position)
        if closest_distance is None or distance < closest_distance:
            closest_callout = callout["name"]
            closest_distance = distance

    return closest_callout


def normalize_player(entity_id, player_dict, map_name=None):
    player_dict = as_dict(player_dict)
    if not player_dict:
        return None

    state = as_dict(player_dict.get("state"))
    match_stats = as_dict(player_dict.get("match_stats"))

    normalized = {
        "entity_id": str(entity_id) if entity_id is not None else None,
        "name": player_dict.get("name"),
        "team": normalize_team(player_dict.get("team")),
        "health": state.get("health"),
        "armor": state.get("armor"),
        "round_kills": state.get("round_kills"),
        "match_kills": match_stats.get("kills"),
        "match_deaths": match_stats.get("deaths"),
        "match_assists": match_stats.get("assists"),
        "map_callout": resolve_map_callout(map_name, player_dict.get("position")),
    }

    return strip_empty(normalized)


def simplify_player_for_snapshot(player_dict, include_combat_stats=False, include_map_callout=False):
    player_dict = as_dict(player_dict)
    simplified = {
        "name": player_dict.get("name"),
        "team": player_dict.get("team"),
    }
    if include_map_callout:
        simplified["map_callout"] = player_dict.get("map_callout")
    if include_combat_stats:
        simplified["round_kills"] = player_dict.get("round_kills")
        simplified["kda"] = strip_empty(
            {
                "kills": player_dict.get("match_kills"),
                "deaths": player_dict.get("match_deaths"),
                "assists": player_dict.get("match_assists"),
            }
        )
    return strip_empty(simplified)


def players_match(left_player, right_player):
    left_player = as_dict(left_player)
    right_player = as_dict(right_player)
    if not left_player or not right_player:
        return False

    left_steamid = left_player.get("steamid")
    right_steamid = right_player.get("steamid")
    if left_steamid and right_steamid:
        return left_steamid == right_steamid

    left_name = left_player.get("name")
    right_name = right_player.get("name")
    return bool(left_name and right_name and left_name == right_name)


def strip_empty(value):
    if isinstance(value, dict):
        cleaned = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [strip_empty(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    return value


def build_player_directory(snapshot):
    by_entity_id = {}
    by_name = {}
    map_name = as_dict(snapshot.get("map")).get("name")

    for entity_id, player in as_dict(snapshot.get("allplayers")).items():
        normalized = normalize_player(entity_id, player, map_name=map_name)
        if not normalized:
            continue
        entity_key = str(entity_id)
        by_entity_id[entity_key] = normalized
        name = normalized.get("name")
        if name and name not in by_name:
            by_name[name] = normalized

    local_player = normalize_player(None, snapshot.get("player"), map_name=map_name)
    if local_player:
        name = local_player.get("name")
        if name and name not in by_name:
            by_name[name] = local_player

    return {
        "by_entity_id": by_entity_id,
        "by_name": by_name,
    }


def get_player_health(player_dict):
    return player_dict.get("health")


def get_round_kills(player_dict):
    return player_dict.get("round_kills")


def get_match_kills(player_dict):
    return player_dict.get("match_kills")


def get_match_deaths(player_dict):
    return player_dict.get("match_deaths")


def collect_important_delta_paths(payload):
    added_paths = sorted(
        set(normalize_path(path) for path in extract_changed_paths(payload.get("added")) if path)
    )
    previous_paths = sorted(
        set(normalize_path(path) for path in extract_changed_paths(payload.get("previously")) if path)
    )

    important_paths = []
    for path in added_paths + previous_paths:
        if path in IMPORTANT_DELTA_EXACT_PATHS:
            important_paths.append(path)
            continue
        if path.endswith(IMPORTANT_DELTA_SUFFIXES):
            important_paths.append(path)
            continue
        if path.endswith(GRENADE_INVENTORY_DELTA_SUFFIXES):
            important_paths.append(path)
            continue
        if path.startswith(("grenades.*.", "allgrenades.*.")) and path in added_paths:
            if path.endswith(GRENADE_DELTA_ALLOWED_SUFFIXES):
                important_paths.append(path)

    return sorted(set(important_paths))


def prune_important_delta_paths(important_paths, events):
    event_types = {event.get("event_type") for event in events}
    pruned = []

    for path in important_paths:
        if path in {"round.bomb", "bomb.state"} and "bomb_event" not in event_types:
            continue
        if path.endswith(("match_stats.kills", "state.round_kills")) and not ({"kill", "kill_cluster"} & event_types):
            continue
        if path.endswith("match_stats.deaths") and "player_death" not in event_types:
            continue
        if path in {"round.phase", "round.win_team", "map.team_ct.score", "map.team_t.score"} and "round_result" not in event_types:
            continue
        if path.startswith(("grenades.*.", "allgrenades.*.")) and "grenade_thrown" not in event_types:
            continue
        if path.startswith("player.weapons.weapon_*.") and "grenade_thrown" not in event_types:
            continue
        pruned.append(path)

    return pruned


def normalize_grenade_state(grenade_id, grenade_dict, block_name, player_directory):
    grenade_dict = as_dict(grenade_dict)
    if not grenade_dict:
        return None

    owner_entity_id = grenade_dict.get("owner")
    owner_player = player_directory["by_entity_id"].get(str(owner_entity_id))

    normalized = {
        "grenade_id": str(grenade_id),
        "source_block": block_name,
        "type": normalize_grenade_type(grenade_dict.get("type") or grenade_dict.get("weapon")),
        "owner_entity_id": str(owner_entity_id) if owner_entity_id not in (None, "") else None,
        "owner_player": owner_player,
        "position": grenade_dict.get("position"),
        "velocity": grenade_dict.get("velocity"),
        "lifetime": grenade_dict.get("lifetime"),
        "effecttime": grenade_dict.get("effecttime"),
        "flames_count": len(as_dict(grenade_dict.get("flames"))),
    }

    return strip_empty(normalized)


def normalize_grenade_type(value):
    if not value:
        return None

    value = str(value).strip().lower()
    mapping = {
        "frag": "frag",
        "hegrenade": "frag",
        "weapon_hegrenade": "frag",
        "flashbang": "flashbang",
        "weapon_flashbang": "flashbang",
        "smoke": "smoke",
        "smokegrenade": "smoke",
        "weapon_smokegrenade": "smoke",
        "molotov": "molotov",
        "weapon_molotov": "molotov",
        "incgrenade": "incendiary",
        "weapon_incgrenade": "incendiary",
        "decoy": "decoy",
        "weapon_decoy": "decoy",
    }
    return mapping.get(value, value.removeprefix("weapon_"))


def grenade_inventory_counts(player_dict):
    player_dict = as_dict(player_dict)
    weapons = as_dict(player_dict.get("weapons"))
    counts = {}

    for weapon in weapons.values():
        weapon = as_dict(weapon)
        grenade_type = normalize_grenade_type(weapon.get("name") or weapon.get("type"))
        if weapon.get("type") != "Grenade" and grenade_type not in {
            "frag",
            "flashbang",
            "smoke",
            "molotov",
            "incendiary",
            "decoy",
        }:
            continue

        count = weapon.get("ammo_reserve")
        if not isinstance(count, int) or count < 1:
            count = 1
        counts[grenade_type] = counts.get(grenade_type, 0) + count

    return counts


def simplify_grenade_for_snapshot(grenade_state):
    grenade_state = as_dict(grenade_state)
    return strip_empty(
        {
            "grenade_type": grenade_state.get("type"),
            "owner_player": simplify_player_for_snapshot(
                grenade_state.get("owner_player"),
                include_map_callout=True,
            ),
        }
    )


def finalize_snapshot_event(event):
    event = as_dict(event)
    event_type = event.get("event_type")

    if event_type == "kill":
        players = as_dict(event.get("players"))
        killer = simplify_player_for_snapshot(
            players.get("killer"),
            include_combat_stats=True,
            include_map_callout=True,
        )
        victim = simplify_player_for_snapshot(players.get("victim"))
        victims = [simplify_player_for_snapshot(victim_entry) for victim_entry in event.get("victims", [])]

        if killer and not victim and not victims:
            return strip_empty(
                {
                    "event_type": "player_scored_kill",
                    "player": killer,
                    "kill_count": event.get("kill_count"),
                }
            )

        if victim and not killer and not victims:
            return strip_empty(
                {
                    "event_type": "player_death",
                    "player": victim,
                }
            )

        return strip_empty(
            {
                "event_type": "kill",
                "killer": killer,
                "victim": victim,
                "victims": victims,
                "kill_count": event.get("kill_count"),
            }
        )

    if event_type == "kill_cluster":
        return strip_empty(
            {
                "event_type": "kill_cluster",
                "kill_count": event.get("total_kill_count"),
                "killers": [
                    simplify_player_for_snapshot(
                        player,
                        include_combat_stats=True,
                        include_map_callout=True,
                    )
                    for player in event.get("killers", [])
                ],
                "victims": [simplify_player_for_snapshot(player) for player in event.get("victims", [])],
            }
        )

    if event_type == "round_result":
        return strip_empty(
            {
                "event_type": "round_result",
                "winner": event.get("winner"),
                "winner_score": event.get("winner_score"),
                "round_phase_after": event.get("round_phase_after"),
                "alive_counts_after": event.get("alive_counts_after"),
            }
        )

    if event_type == "team_counter":
        return strip_empty(
            {
                "event_type": "team_counter",
                "alive_counts_after": event.get("alive_counts_after"),
            }
        )

    if event_type == "bomb_event":
        return strip_empty(
            {
                "event_type": "bomb_event",
                "state_after": event.get("state_after"),
            }
        )

    if event_type == "player_death":
        return strip_empty(
            {
                "event_type": "player_death",
                "player": simplify_player_for_snapshot(event.get("player"), include_combat_stats=True),
            }
        )

    if event_type == "grenade_thrown":
        return strip_empty(
            {
                "event_type": "grenade_thrown",
                **simplify_grenade_for_snapshot(event.get("grenade")),
            }
        )

    return strip_empty({"event_type": event_type})


def build_grenade_thrown_events(previous_snapshot, current_snapshot):
    current_player_directory = build_player_directory(current_snapshot)
    events = []

    for block_name in ("grenades", "allgrenades"):
        previous_block = as_dict(previous_snapshot.get(block_name))
        current_block = as_dict(current_snapshot.get(block_name))

        for grenade_id, current_grenade in current_block.items():
            if grenade_id in previous_block:
                continue

            grenade_state = normalize_grenade_state(
                grenade_id,
                current_grenade,
                block_name,
                current_player_directory,
            )
            if not grenade_state:
                continue

            events.append(
                {
                    "event_type": "grenade_thrown",
                    "association": {
                        "status": "owner_resolved" if grenade_state.get("owner_player") else "owner_unresolved",
                        "method": "live_grenade_entity_appeared",
                    },
                    "grenade": grenade_state,
                }
            )

    previous_local_player_raw = previous_snapshot.get("player")
    current_local_player_raw = current_snapshot.get("player")
    previous_round = as_dict(previous_snapshot.get("round"))
    current_round = as_dict(current_snapshot.get("round"))
    round_transitioned_to_end = (
        previous_round.get("phase") != current_round.get("phase")
        and current_round.get("phase") in ROUND_END_PHASES
    )

    if players_match(previous_local_player_raw, current_local_player_raw) and not round_transitioned_to_end:
        previous_local_player = normalize_player(
            None,
            previous_local_player_raw,
            map_name=as_dict(previous_snapshot.get("map")).get("name"),
        )
        current_local_player = normalize_player(
            None,
            current_local_player_raw,
            map_name=as_dict(current_snapshot.get("map")).get("name"),
        )
        previous_counts = grenade_inventory_counts(previous_local_player_raw)
        current_counts = grenade_inventory_counts(current_local_player_raw)
        local_death = collect_local_player_death(previous_snapshot, current_snapshot)

        if previous_local_player and current_local_player and not local_death:
            for grenade_type in sorted(set(previous_counts) | set(current_counts)):
                previous_count = previous_counts.get(grenade_type, 0)
                current_count = current_counts.get(grenade_type, 0)
                thrown_count = previous_count - current_count
                if thrown_count <= 0:
                    continue

                duplicate_entity_event = any(
                    event.get("event_type") == "grenade_thrown"
                    and as_dict(event.get("grenade")).get("type") == grenade_type
                    and as_dict(as_dict(event.get("grenade")).get("owner_player")).get("name")
                    == current_local_player.get("name")
                    for event in events
                )
                if duplicate_entity_event:
                    continue

                events.append(
                    strip_empty(
                        {
                            "event_type": "grenade_thrown",
                            "association": {
                                "status": "owner_resolved",
                                "method": "local_grenade_inventory_decreased",
                            },
                            "grenade": {
                                "type": grenade_type,
                                "owner_player": current_local_player,
                            },
                            "throw_count": thrown_count,
                        }
                    )
                )

    return events


def collect_player_deaths(previous_snapshot, current_snapshot):
    previous_players = build_player_directory(previous_snapshot)["by_entity_id"]
    current_players = build_player_directory(current_snapshot)["by_entity_id"]

    deaths = []
    for entity_id in sorted(set(previous_players) | set(current_players)):
        previous_player = previous_players.get(entity_id)
        current_player = current_players.get(entity_id)
        if not previous_player or not current_player:
            continue

        previous_health = get_player_health(previous_player)
        current_health = get_player_health(current_player)
        if (
            isinstance(previous_health, (int, float))
            and isinstance(current_health, (int, float))
            and previous_health > 0
            and current_health <= 0
        ):
            deaths.append(
                {
                    "victim_before": previous_player,
                    "victim_after": current_player,
                }
            )

    return deaths


def collect_kill_increments(previous_snapshot, current_snapshot):
    previous_players = build_player_directory(previous_snapshot)["by_entity_id"]
    current_players = build_player_directory(current_snapshot)["by_entity_id"]

    increments = []
    for entity_id in sorted(set(previous_players) | set(current_players)):
        previous_player = previous_players.get(entity_id)
        current_player = current_players.get(entity_id)
        if not previous_player or not current_player:
            continue

        previous_round_kills = get_round_kills(previous_player)
        current_round_kills = get_round_kills(current_player)
        previous_match_kills = get_match_kills(previous_player)
        current_match_kills = get_match_kills(current_player)

        round_delta = (
            current_round_kills - previous_round_kills
            if isinstance(previous_round_kills, int) and isinstance(current_round_kills, int)
            else 0
        )
        match_delta = (
            current_match_kills - previous_match_kills
            if isinstance(previous_match_kills, int) and isinstance(current_match_kills, int)
            else 0
        )
        kill_count_delta = max(round_delta, match_delta)

        if kill_count_delta > 0:
            increments.append(
                {
                    "killer_after": current_player,
                    "killer_before": previous_player,
                    "kill_count_delta": kill_count_delta,
                    "round_kills_before": previous_round_kills,
                    "round_kills_after": current_round_kills,
                    "match_kills_before": previous_match_kills,
                    "match_kills_after": current_match_kills,
                }
            )

    return increments


def collect_local_player_kill_increment(previous_snapshot, current_snapshot):
    previous_player_raw = previous_snapshot.get("player")
    current_player_raw = current_snapshot.get("player")
    if not players_match(previous_player_raw, current_player_raw):
        return None

    previous_player = normalize_player(
        None,
        previous_player_raw,
        map_name=as_dict(previous_snapshot.get("map")).get("name"),
    )
    current_player = normalize_player(
        None,
        current_player_raw,
        map_name=as_dict(current_snapshot.get("map")).get("name"),
    )
    if not previous_player or not current_player:
        return None

    previous_round_kills = get_round_kills(previous_player)
    current_round_kills = get_round_kills(current_player)
    previous_match_kills = get_match_kills(previous_player)
    current_match_kills = get_match_kills(current_player)

    round_delta = (
        current_round_kills - previous_round_kills
        if isinstance(previous_round_kills, int) and isinstance(current_round_kills, int)
        else 0
    )
    match_delta = (
        current_match_kills - previous_match_kills
        if isinstance(previous_match_kills, int) and isinstance(current_match_kills, int)
        else 0
    )
    kill_count_delta = max(round_delta, match_delta)

    if kill_count_delta <= 0:
        return None

    return {
        "killer_after": current_player,
        "killer_before": previous_player,
        "kill_count_delta": kill_count_delta,
        "round_kills_before": previous_round_kills,
        "round_kills_after": current_round_kills,
        "match_kills_before": previous_match_kills,
        "match_kills_after": current_match_kills,
    }


def collect_local_player_death(previous_snapshot, current_snapshot):
    previous_player_raw = previous_snapshot.get("player")
    current_player_raw = current_snapshot.get("player")
    if not players_match(previous_player_raw, current_player_raw):
        return None

    previous_player = normalize_player(
        None,
        previous_player_raw,
        map_name=as_dict(previous_snapshot.get("map")).get("name"),
    )
    current_player = normalize_player(
        None,
        current_player_raw,
        map_name=as_dict(current_snapshot.get("map")).get("name"),
    )
    if not previous_player or not current_player:
        return None

    previous_match_deaths = get_match_deaths(previous_player)
    current_match_deaths = get_match_deaths(current_player)
    deaths_delta = (
        current_match_deaths - previous_match_deaths
        if isinstance(previous_match_deaths, int) and isinstance(current_match_deaths, int)
        else 0
    )

    previous_health = get_player_health(previous_player)
    current_health = get_player_health(current_player)
    died_from_health = (
        isinstance(previous_health, (int, float))
        and isinstance(current_health, (int, float))
        and previous_health > 0
        and current_health <= 0
    )

    if deaths_delta <= 0 and not died_from_health:
        return None

    return {
        "event_type": "player_death",
        "player": current_player,
    }


def build_kill_cluster_event(increments, deaths):
    killers = [increment["killer_after"] for increment in increments]
    victims = [death["victim_after"] for death in deaths]
    total_kill_count = sum(increment["kill_count_delta"] for increment in increments)

    return strip_empty(
        {
            "event_type": "kill_cluster",
            "association": {
                "status": "ambiguous_multi_actor",
                "method": "kill_deltas_and_health_drops",
            },
            "total_kill_count": total_kill_count,
            "killers": killers,
            "victims": victims,
        }
    )


def increment_matches_player(increment, player_dict):
    increment_player = increment.get("killer_after") or {}
    player_dict = player_dict or {}
    increment_entity = increment_player.get("entity_id")
    player_entity = player_dict.get("entity_id")
    if increment_entity and player_entity:
        return increment_entity == player_entity
    increment_name = increment_player.get("name")
    player_name = player_dict.get("name")
    return bool(increment_name and player_name and increment_name == player_name)


def build_kill_events(previous_snapshot, current_snapshot):
    deaths = collect_player_deaths(previous_snapshot, current_snapshot)
    increments = collect_kill_increments(previous_snapshot, current_snapshot)
    local_increment = collect_local_player_kill_increment(previous_snapshot, current_snapshot)
    if local_increment and not any(increment_matches_player(increment, local_increment["killer_after"]) for increment in increments):
        increments.append(local_increment)

    events = []

    if not increments and not deaths:
        return events

    if len(increments) == 1:
        increment = increments[0]
        killer = increment["killer_after"]
        killer_team = killer.get("team")
        opposite_deaths = [
            death for death in deaths if death["victim_after"].get("team") in TEAM_NAMES and death["victim_after"].get("team") != killer_team
        ]
        total_kill_count = increment["kill_count_delta"]

        if total_kill_count == 1 and len(opposite_deaths) == 1:
            matched_death = opposite_deaths[0]
            events.append(
                strip_empty(
                    {
                        "event_type": "kill",
                        "association": {
                            "status": "paired",
                            "method": "single_kill_delta_and_health_drop",
                        },
                        "players": {
                            "killer": killer,
                            "victim": matched_death["victim_after"],
                        },
                        "killer_round_kills_after": increment["round_kills_after"],
                        "killer_match_kills_after": increment["match_kills_after"],
                    }
                )
            )
            return events

        if total_kill_count > 1 and len(opposite_deaths) == total_kill_count:
            events.append(
                strip_empty(
                    {
                        "event_type": "kill",
                        "association": {
                            "status": "paired_multi_kill",
                            "method": "single_killer_multi_kill_delta_and_health_drop",
                        },
                        "killer": killer,
                        "victims": [death["victim_after"] for death in opposite_deaths],
                        "kill_count": total_kill_count,
                        "killer_round_kills_after": increment["round_kills_after"],
                        "killer_match_kills_after": increment["match_kills_after"],
                    }
                )
            )
            return events

        if total_kill_count > 0 and not deaths:
            events.append(
                strip_empty(
                    {
                        "event_type": "kill",
                        "association": {
                            "status": "killer_only",
                            "method": "kill_delta_without_visible_victim",
                        },
                        "players": {
                            "killer": killer,
                        },
                        "kill_count": total_kill_count,
                        "killer_round_kills_after": increment["round_kills_after"],
                        "killer_match_kills_after": increment["match_kills_after"],
                    }
                )
            )
            return events

        if total_kill_count > 0 or deaths:
            events.append(build_kill_cluster_event(increments, deaths))
            return events

    if len(increments) > 1 or len(deaths) > 1:
        events.append(build_kill_cluster_event(increments, deaths))
        return events

    for increment in increments:
        events.append(
            strip_empty(
                {
                    "event_type": "kill",
                    "association": {
                        "status": "killer_only",
                        "method": "kill_delta_without_unambiguous_victim",
                    },
                    "players": {
                        "killer": increment["killer_after"],
                    },
                    "kill_count": increment["kill_count_delta"],
                    "killer_round_kills_after": increment["round_kills_after"],
                    "killer_match_kills_after": increment["match_kills_after"],
                }
            )
        )

    for death in deaths:
        events.append(
            strip_empty(
                {
                    "event_type": "kill",
                    "association": {
                        "status": "victim_only",
                        "method": "health_drop_without_unambiguous_killer",
                    },
                    "players": {
                        "victim": death["victim_after"],
                    },
                    "victim_health_after": get_player_health(death["victim_after"]),
                }
            )
        )

    return events


def build_local_player_death_events(previous_snapshot, current_snapshot):
    if as_dict(current_snapshot.get("allplayers")):
        return []

    local_death = collect_local_player_death(previous_snapshot, current_snapshot)
    if not local_death:
        return []

    return [strip_empty(local_death)]


def build_bomb_events(previous_snapshot, current_snapshot):
    previous_round = as_dict(previous_snapshot.get("round"))
    current_round = as_dict(current_snapshot.get("round"))
    previous_bomb = as_dict(previous_snapshot.get("bomb"))
    current_bomb = as_dict(current_snapshot.get("bomb"))

    previous_round_bomb = previous_round.get("bomb")
    current_round_bomb = current_round.get("bomb")
    previous_bomb_state = previous_bomb.get("state")
    current_bomb_state = current_bomb.get("state")

    state_after = current_round_bomb or current_bomb_state
    if state_after not in {"planted", "defused", "exploded"}:
        return []

    if previous_round_bomb == current_round_bomb and previous_bomb_state == current_bomb_state:
        return []

    return [
        strip_empty(
            {
                "event_type": "bomb_event",
                "state_after": state_after,
            }
        )
    ]


def build_round_result_events(previous_snapshot, current_snapshot):
    previous_round = as_dict(previous_snapshot.get("round"))
    current_round = as_dict(current_snapshot.get("round"))
    previous_score = compute_score(previous_snapshot)
    current_score = compute_score(current_snapshot)
    previous_phase = previous_round.get("phase")
    current_phase = current_round.get("phase")
    previous_win_team = normalize_team(previous_round.get("win_team"))
    current_win_team = normalize_team(current_round.get("win_team"))

    phase_entered_round_end = previous_phase != current_phase and current_phase in ROUND_END_PHASES
    winner_changed = previous_win_team != current_win_team and current_win_team in TEAM_NAMES
    score_increment_team = None
    for score_key, team_name in [("ct", "CT"), ("t", "T")]:
        if current_score[score_key] > previous_score[score_key]:
            score_increment_team = team_name
            break

    if current_phase == "freezetime":
        return []

    if not phase_entered_round_end and not winner_changed and score_increment_team is None:
        return []

    winner = current_win_team or previous_win_team or score_increment_team
    winner_score = None
    if winner == "CT":
        winner_score = current_score["ct"]
    elif winner == "T":
        winner_score = current_score["t"]

    alive_counts_after = None
    if current_phase == "over":
        alive_counts_after = compute_alive_counts(current_snapshot)

    return [
        strip_empty(
            {
                "event_type": "round_result",
                "winner": winner,
                "round_phase_after": current_phase,
                "winner_score": winner_score,
                "alive_counts_after": alive_counts_after,
            }
        )
    ]


def build_team_counter_events(previous_snapshot, current_snapshot):
    current_round = as_dict(current_snapshot.get("round"))
    if current_round.get("phase") == "freezetime":
        return []

    previous_alive = compute_alive_counts(previous_snapshot)
    current_alive = compute_alive_counts(current_snapshot)
    if previous_alive == current_alive:
        return []

    changed_counts_after = {}
    for team in TEAM_NAMES:
        if previous_alive[team] != current_alive[team]:
            changed_counts_after[team] = current_alive[team]

    return [
        {
            "event_type": "team_counter",
            "alive_counts_after": changed_counts_after,
        }
    ]


def prune_bomb_explosion_victim_events(events):
    exploded = any(
        as_dict(event).get("event_type") == "bomb_event"
        and as_dict(event).get("state_after") == "exploded"
        for event in events
    )
    if not exploded:
        return events

    return [
        event
        for event in events
        if as_dict(event).get("event_type") not in {"kill", "kill_cluster", "player_death"}
    ]


def prune_standalone_team_counter_events(events):
    if not events:
        return events

    non_team_counter_events = [
        event for event in events if as_dict(event).get("event_type") != "team_counter"
    ]
    if non_team_counter_events:
        return events

    return []


def filter_important_events(previous_snapshot, current_snapshot, payload_sequence, payload=None):
    if not previous_snapshot or not current_snapshot:
        return {
            "events": [],
        }

    payload = payload or {}
    events = []
    events.extend(build_kill_events(previous_snapshot, current_snapshot))
    events.extend(build_local_player_death_events(previous_snapshot, current_snapshot))
    events.extend(build_bomb_events(previous_snapshot, current_snapshot))
    events.extend(build_grenade_thrown_events(previous_snapshot, current_snapshot))
    round_result_events = build_round_result_events(previous_snapshot, current_snapshot)
    events.extend(round_result_events)
    if not round_result_events:
        events.extend(build_team_counter_events(previous_snapshot, current_snapshot))
    events = prune_bomb_explosion_victim_events(events)
    events = prune_standalone_team_counter_events(events)

    created_at = now_stamp()
    for index, event in enumerate(events, start=1):
        event["event_index"] = index

    finalized_events = [finalize_snapshot_event(event) for event in events]

    return {
        "created_at": created_at,
        "events": finalized_events,
    }


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


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
            payload_sequence = PIPELINE_STATE.payload_count
            previous_snapshot = copy.deepcopy(PIPELINE_STATE.previous_snapshot)
            current_snapshot = copy.deepcopy(PIPELINE_STATE.latest_snapshot)

        raw_record = {
            "received_at": now_stamp(),
            "payload_sequence": payload_sequence,
            "payload": payload,
        }
        append_pretty_json_record(
            RAW_GSI_PATH,
            raw_record,
        )
        write_pretty_json_file(RAW_GSI_LATEST_PATH, raw_record)

        filtered_batch = filter_important_events(
            previous_snapshot,
            current_snapshot,
            payload_sequence=payload_sequence,
            payload=payload,
        )

        if filtered_batch["events"]:
            append_pretty_json_record(FILTERED_EVENTS_PATH, filtered_batch)
            write_pretty_json_file(FILTERED_EVENTS_LATEST_PATH, filtered_batch)
            print(
                f"[gsi] #{payload_sequence} stored raw payload and emitted "
                f"{len(filtered_batch['events'])} filtered event(s) -> prompt queued",
                flush=True,
            )
        else:
            print(f"[gsi] #{payload_sequence} stored raw payload with no important events", flush=True)

        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except OSError:
            return

        if filtered_batch["events"]:
            run_prompt_runtime_async(copy.deepcopy(filtered_batch), payload_sequence=payload_sequence)

    def log_message(self, format, *args):
        return


def main():
    reset_session_files()
    reset_prompt_runtime_state()
    PIPELINE_STATE.latest_snapshot = None
    PIPELINE_STATE.previous_snapshot = None
    PIPELINE_STATE.payload_count = 0
    append_log(f"[{now_stamp()}] pipeline v2 session started\n")

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Raw GSI history:    {RAW_GSI_PATH}", flush=True)
    print(f"Raw GSI latest:     {RAW_GSI_LATEST_PATH}", flush=True)
    print(f"Filtered history:   {FILTERED_EVENTS_PATH}", flush=True)
    print(f"Filtered latest:    {FILTERED_EVENTS_LATEST_PATH}", flush=True)
    print(f"Prompt runtime:     {PROMPT_RUNTIME_HISTORY_PATH}", flush=True)
    print(f"Prompt latest:      {PROMPT_RUNTIME_LATEST_PATH}", flush=True)
    print(f"Pipeline log:       {PIPELINE_LOG}", flush=True)
    print(
        "This v2 listener keeps pretty-printed history files and overwrite-on-update "
        "latest files for raw GSI, filtered events, and immediate prompt runtime results.",
        flush=True,
    )

    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
