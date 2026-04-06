import copy
import hashlib
import json
import os
import queue
import random
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
REPO_ROOT = SCRIPT_DIR.parent


def env_text(name, default=""):
    return os.environ.get(name, ENV_FILE_VALUES.get(name, default))


def env_bool(name, default=False):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_float(name, default):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_int(name, default):
    value = os.environ.get(name, ENV_FILE_VALUES.get(name))
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def parse_env_file(path_str):
    path = Path(path_str).expanduser().resolve()
    values = {}

    if not path.exists():
        return values

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key:
            values[key] = value

    return values


EXPECTED_TOKEN = env_text("CS2_GSI_AUTH_TOKEN", "") or None
HOST = env_text("CS2_GSI_HOST", "127.0.0.1")
PORT = env_int("CS2_GSI_PORT", 3000)

ENABLE_COMMENTARY = env_bool("CS2_GSI_ENABLE_COMMENTARY", False)
COMMENTARY_ONLY_ON_EVENTS = env_bool("CS2_GSI_COMMENTARY_ONLY_ON_EVENTS", True)
COMMENTARY_MIN_INTERVAL = env_float("CS2_GSI_COMMENTARY_MIN_INTERVAL", 2.0)
MODEL_TIMEOUT = env_float("CS2_GSI_MODEL_TIMEOUT", 4.0)
QUIET_IDLE_SECONDS = env_float("QUIET_IDLE_SECONDS", 5.0)
QUIET_COMMENTARY_INTERVAL = env_float("QUIET_COMMENTARY_INTERVAL", 5.0)

MODEL_API_BASE = env_text("MODEL_API_BASE", "http://127.0.0.1:12434").rstrip("/")
MODEL_NAME = env_text(
    "MODEL_NAME", "hf.co/Qwen/Qwen2.5-0.5B-Instruct-GGUF:Q4_K_M"
)
SYSTEM_PROMPT = env_text(
    "SYSTEM_PROMPT",
    (
        "You are an esports commentator for a live Counter-Strike 2 broadcast. "
        "Use only the supplied match state. Output one or two short, high-energy "
        "caster lines. No markdown. No bullet points. No reasoning. No filler."
    ),
)
PLAY_BY_PLAY_SYSTEM_PROMPT = env_text(
    "PLAY_BY_PLAY_SYSTEM_PROMPT",
    (
        "You are the play-by-play caster on a live Counter-Strike 2 broadcast. "
        "Call only the freshest action from the supplied state. Output one very short, "
        "high-energy line. No markdown. No bullet points. No reasoning. No filler."
    ),
)
COLOR_SYSTEM_PROMPT = env_text(
    "COLOR_SYSTEM_PROMPT",
    (
        "You are the color commentator on a live Counter-Strike 2 broadcast. "
        "Add one short follow-up line that explains why the moment matters, the tactical "
        "read, or the momentum shift. Do not repeat the play-by-play wording. "
        "No markdown. No bullet points. No reasoning preamble."
    ),
)
CHEMISTRY_SYSTEM_PROMPT = env_text(
    "CHEMISTRY_SYSTEM_PROMPT",
    (
        "You are part of a two-person Counter-Strike 2 casting duo filling a quiet moment "
        "in the round. Speak naturally, briefly, and conversationally from the supplied state. "
        "No markdown. No bullet points. No filler."
    ),
)
TEMPERATURE = env_float("TEMPERATURE", 0.4)
MAX_TOKENS = env_int("MAX_TOKENS", 100)
SERVER_URL = env_text("SERVER_URL", "ws://localhost:8091/v1/audio/speech/stream")
VOICE_NAME = env_text("VOICE_NAME", "")
SECONDARY_VOICE_NAME = env_text("SECONDARY_VOICE_NAME", "")
SECONDARY_VOICE_PROBABILITY = env_float("SECONDARY_VOICE_PROBABILITY", 0.25)
VOICE_SELECTION_MODE = env_text("VOICE_SELECTION_MODE", "mono").strip().lower()
DUAL_VOICE_HEURISTIC = env_text("DUAL_VOICE_HEURISTIC", "random").strip().lower()
COLOR_FOLLOWUP_DELAY = env_float("COLOR_FOLLOWUP_DELAY", 2.0)
VOICE_CONFIG_FILE = env_text("VOICE_CONFIG_FILE", "")
TTS_STREAM_SCRIPT = env_text(
    "TTS_STREAM_SCRIPT", str(REPO_ROOT / "tts-io" / "stream_tts.py")
)
VOICE_MANIFEST_FILE = env_text(
    "VOICE_MANIFEST_FILE",
    str(Path(TTS_STREAM_SCRIPT).expanduser().resolve().parent / "voices" / "generated" / "voices.json"),
)
TTS_PYTHON = env_text("TTS_PYTHON", str(REPO_ROOT / ".venv" / "bin" / "python"))

STATE_LOCK = threading.Lock()
LATEST_SNAPSHOT = {}
LAST_PROMPT_HASH = None
LAST_COMMENTARY_AT = 0.0
COMMENTARY_QUEUE = queue.PriorityQueue(maxsize=16)
SPEAKER_EMBEDDING_FILES = {}
LAST_SELECTED_VOICE = None
LAST_LINES_BY_VOICE = {}
LAST_EVENT_AT = 0.0
EVENT_SEQUENCE = 0
LAST_QUIET_COMMENTARY_AT = 0.0
LAST_QUIET_SPEAKER = None
JOB_COUNTER = 0
RECENT_BOOTH_LINES = []


def tts_workspace_name():
    return Path(TTS_STREAM_SCRIPT).expanduser().resolve().parent.name


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def as_dict(value):
    return value if isinstance(value, dict) else {}


def as_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def as_float(value):
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def compact_number(value):
    if value is None:
        return None
    if isinstance(value, float):
        return round(value, 2)
    return value


def deep_merge(base, incoming):
    if not isinstance(base, dict) or not isinstance(incoming, dict):
        return copy.deepcopy(incoming)

    merged = copy.deepcopy(base)
    for key, value in incoming.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def prune_empty(value):
    if isinstance(value, dict):
        pruned = {}
        for key, item in value.items():
            cleaned = prune_empty(item)
            if cleaned not in (None, "", [], {}, ()):
                pruned[key] = cleaned
        return pruned

    if isinstance(value, list):
        pruned = [prune_empty(item) for item in value]
        return [item for item in pruned if item not in (None, "", [], {}, ())]

    return value


def format_time_seconds(value):
    numeric = as_float(value)
    if numeric is None:
        return None
    return round(numeric, 2)


def extract_player(snapshot):
    return as_dict(snapshot.get("player"))


def extract_allplayers(snapshot):
    return as_dict(snapshot.get("allplayers"))


def player_name(player, fallback="player"):
    return player.get("name") or player.get("steamid") or fallback


def weapon_entries(weapons):
    items = []
    for weapon in as_dict(weapons).values():
        if isinstance(weapon, dict):
            items.append(weapon)
    return items


def active_weapon_name(weapons):
    for weapon in weapon_entries(weapons):
        if weapon.get("state") == "active":
            return weapon.get("name") or weapon.get("type")
    return None


def summarize_weapons(weapons):
    summaries = []
    active_name = None

    for weapon in weapon_entries(weapons):
        name = weapon.get("name") or weapon.get("type")
        if not name:
            continue

        clip = as_int(weapon.get("ammo_clip"))
        reserve = as_int(weapon.get("ammo_reserve"))
        ammo = None
        if clip is not None and reserve is not None:
            ammo = f"{clip}/{reserve}"
        elif clip is not None:
            ammo = str(clip)

        label = name
        if ammo:
            label = f"{label} {ammo}"

        state = weapon.get("state")
        if state == "active":
            active_name = name
            label = f"{label} [active]"

        summaries.append(label)

    summaries.sort(key=lambda item: "[active]" not in item)
    return summaries, active_name


def summarize_round_wins(map_data):
    round_wins = as_dict(map_data.get("round_wins"))
    if not round_wins:
        return []

    def sort_key(key):
        text = str(key)
        return int(text) if text.isdigit() else 9999

    recent = []
    for key in sorted(round_wins.keys(), key=sort_key)[-5:]:
        recent.append({"round": str(key), "result": round_wins[key]})
    return recent


def summarize_player_block(player):
    state = as_dict(player.get("state"))
    stats = as_dict(player.get("match_stats"))
    weapons, active_weapon = summarize_weapons(player.get("weapons"))

    position = player.get("position")
    forward = player.get("forward")

    summary = {
        "name": player.get("name"),
        "steamid": player.get("steamid"),
        "team": player.get("team"),
        "observer_slot": player.get("observer_slot"),
        "activity": player.get("activity"),
        "state": {
            "health": as_int(state.get("health")),
            "armor": as_int(state.get("armor")),
            "helmet": state.get("helmet"),
            "money": as_int(state.get("money")),
            "round_kills": as_int(state.get("round_kills")),
            "round_killhs": as_int(state.get("round_killhs")),
            "flashed": compact_number(as_float(state.get("flashed"))),
            "smoked": compact_number(as_float(state.get("smoked"))),
            "burning": compact_number(as_float(state.get("burning"))),
            "equip_value": as_int(state.get("equip_value")),
            "defusekit": state.get("defusekit"),
        },
        "match_stats": {
            "kills": as_int(stats.get("kills")),
            "assists": as_int(stats.get("assists")),
            "deaths": as_int(stats.get("deaths")),
            "mvps": as_int(stats.get("mvps")),
            "score": as_int(stats.get("score")),
        },
        "position": position,
        "forward": forward,
        "active_weapon": active_weapon,
        "weapons": weapons[:6],
    }

    return prune_empty(summary)


def summarize_allplayers_block(allplayers):
    if not allplayers:
        return {}

    alive = {}
    top_players = []

    for slot, player in allplayers.items():
        player = as_dict(player)
        team = player.get("team")
        state = as_dict(player.get("state"))
        stats = as_dict(player.get("match_stats"))
        health = as_int(state.get("health"))

        if team and health and health > 0:
            alive[team] = alive.get(team, 0) + 1

        top_players.append(
            {
                "slot": slot,
                "name": player_name(player, f"slot_{slot}"),
                "team": team,
                "health": health,
                "kills": as_int(stats.get("kills")),
                "deaths": as_int(stats.get("deaths")),
                "active_weapon": active_weapon_name(player.get("weapons")),
            }
        )

    top_players.sort(
        key=lambda item: (
            -(item.get("kills") or -1),
            item.get("deaths") or 999,
            item.get("name") or "",
        )
    )

    return prune_empty(
        {
            "alive_by_team": alive,
            "top_players": top_players[:5],
            "tracked_players": len(top_players),
        }
    )


def summarize_grenades_block(allgrenades):
    allgrenades = as_dict(allgrenades)
    if not allgrenades:
        return {}

    counts = {}
    samples = []

    for grenade in allgrenades.values():
        grenade = as_dict(grenade)
        grenade_type = grenade.get("type") or "unknown"
        counts[grenade_type] = counts.get(grenade_type, 0) + 1
        if len(samples) < 5:
            samples.append(
                prune_empty(
                    {
                        "type": grenade_type,
                        "owner": grenade.get("owner"),
                        "lifetime": format_time_seconds(grenade.get("lifetime")),
                        "position": grenade.get("position"),
                        "velocity": grenade.get("velocity"),
                    }
                )
            )

    return prune_empty({"counts": counts, "samples": samples})


def build_llm_payload(snapshot, events):
    map_data = as_dict(snapshot.get("map"))
    round_data = as_dict(snapshot.get("round"))
    bomb_data = as_dict(snapshot.get("bomb"))
    countdowns = as_dict(snapshot.get("phase_countdowns"))
    player = extract_player(snapshot)
    allplayers = extract_allplayers(snapshot)

    payload = {
        "timestamp": now_stamp(),
        "events": events[:8],
        "state": {
            "provider": prune_empty(
                {
                    "name": as_dict(snapshot.get("provider")).get("name"),
                    "appid": as_dict(snapshot.get("provider")).get("appid"),
                    "version": as_dict(snapshot.get("provider")).get("version"),
                    "steamid": as_dict(snapshot.get("provider")).get("steamid"),
                }
            ),
            "map": prune_empty(
                {
                    "name": map_data.get("name"),
                    "mode": map_data.get("mode"),
                    "phase": map_data.get("phase"),
                    "round": as_int(map_data.get("round")),
                    "team_ct": prune_empty(
                        {
                            "score": as_int(as_dict(map_data.get("team_ct")).get("score")),
                            "timeouts_remaining": as_int(
                                as_dict(map_data.get("team_ct")).get("timeouts_remaining")
                            ),
                        }
                    ),
                    "team_t": prune_empty(
                        {
                            "score": as_int(as_dict(map_data.get("team_t")).get("score")),
                            "timeouts_remaining": as_int(
                                as_dict(map_data.get("team_t")).get("timeouts_remaining")
                            ),
                        }
                    ),
                    "recent_round_wins": summarize_round_wins(map_data),
                }
            ),
            "round": prune_empty(
                {
                    "phase": round_data.get("phase"),
                    "win_team": round_data.get("win_team"),
                    "bomb": round_data.get("bomb"),
                    "phase_countdowns": prune_empty(
                        {
                            "phase": countdowns.get("phase"),
                            "phase_ends_in": format_time_seconds(
                                countdowns.get("phase_ends_in")
                            ),
                        }
                    ),
                }
            ),
            "bomb": prune_empty(
                {
                    "state": bomb_data.get("state"),
                    "player": bomb_data.get("player"),
                    "position": bomb_data.get("position"),
                    "countdown": format_time_seconds(bomb_data.get("countdown")),
                }
            ),
            "player": summarize_player_block(player),
            "observer": summarize_allplayers_block(allplayers),
            "grenades": summarize_grenades_block(snapshot.get("allgrenades")),
        },
    }

    return prune_empty(payload)


def render_summary(payload):
    state = as_dict(payload.get("state"))
    map_data = as_dict(state.get("map"))
    round_data = as_dict(state.get("round"))
    player = as_dict(state.get("player"))
    player_state = as_dict(player.get("state"))
    player_stats = as_dict(player.get("match_stats"))
    observer = as_dict(state.get("observer"))
    bomb = as_dict(state.get("bomb"))
    grenades = as_dict(state.get("grenades"))

    lines = [f"=== CS2 GSI Update @ {payload.get('timestamp')} ==="]

    events = payload.get("events") or []
    if events:
        lines.append("Events:")
        for event in events:
            lines.append(f"- {event}")

    map_name = map_data.get("name") or "unknown"
    map_phase = map_data.get("phase") or "unknown"
    ct_score = as_dict(map_data.get("team_ct")).get("score")
    t_score = as_dict(map_data.get("team_t")).get("score")
    lines.append(
        f"Map: {map_name} | phase={map_phase} | score CT {ct_score} - T {t_score}"
    )

    round_phase = round_data.get("phase")
    time_left = as_dict(round_data.get("phase_countdowns")).get("phase_ends_in")
    if round_phase or time_left is not None:
        lines.append(f"Round: phase={round_phase} | phase_ends_in={time_left}")

    if bomb:
        lines.append(
            "Bomb: "
            f"state={bomb.get('state')} player={bomb.get('player')} "
            f"countdown={bomb.get('countdown')}"
        )

    if player:
        lines.append(
            "Player: "
            f"{player.get('name')} team={player.get('team')} "
            f"hp={player_state.get('health')} armor={player_state.get('armor')} "
            f"money={player_state.get('money')} active={player.get('active_weapon')}"
        )
        lines.append(
            "Stats: "
            f"{player_stats.get('kills')}/{player_stats.get('assists')}/{player_stats.get('deaths')} "
            f"mvps={player_stats.get('mvps')} score={player_stats.get('score')}"
        )
        if player.get("weapons"):
            lines.append("Weapons: " + ", ".join(player["weapons"]))

    if observer:
        alive_by_team = observer.get("alive_by_team")
        if alive_by_team:
            lines.append(
                "Observer: alive "
                + ", ".join(f"{team}={count}" for team, count in alive_by_team.items())
            )
        top_players = observer.get("top_players") or []
        if top_players:
            lines.append(
                "Top players: "
                + ", ".join(
                    f"{entry.get('name')} {entry.get('kills')}/{entry.get('deaths')}"
                    for entry in top_players[:3]
                )
            )

    if grenades:
        counts = grenades.get("counts") or {}
        if counts:
            lines.append(
                "Grenades: "
                + ", ".join(f"{grenade_type}={count}" for grenade_type, count in counts.items())
            )

    return "\n".join(lines)


def detect_score_event(previous_map, current_map):
    prev_ct = as_int(as_dict(previous_map.get("team_ct")).get("score"))
    prev_t = as_int(as_dict(previous_map.get("team_t")).get("score"))
    curr_ct = as_int(as_dict(current_map.get("team_ct")).get("score"))
    curr_t = as_int(as_dict(current_map.get("team_t")).get("score"))

    if (prev_ct, prev_t) == (curr_ct, curr_t):
        return None

    if curr_ct is None or curr_t is None:
        return None

    return f"score update: CT {curr_ct} - T {curr_t}"


def detect_allplayer_events(previous_allplayers, current_allplayers, skip_names=None):
    events = []
    skip_names = skip_names or set()

    for slot, current in current_allplayers.items():
        previous = as_dict(previous_allplayers.get(slot))
        current = as_dict(current)

        name = player_name(current, f"slot_{slot}")
        if name in skip_names:
            continue

        prev_stats = as_dict(previous.get("match_stats"))
        curr_stats = as_dict(current.get("match_stats"))

        prev_kills = as_int(prev_stats.get("kills")) or 0
        curr_kills = as_int(curr_stats.get("kills")) or 0
        if curr_kills > prev_kills:
            delta = curr_kills - prev_kills
            events.append(f"{name} picked up {delta} kill{'s' if delta != 1 else ''}")

        prev_deaths = as_int(prev_stats.get("deaths")) or 0
        curr_deaths = as_int(curr_stats.get("deaths")) or 0
        if curr_deaths > prev_deaths:
            events.append(f"{name} goes down")

    return events


def detect_events(previous_snapshot, current_snapshot):
    if not previous_snapshot:
        return []

    events = []

    previous_map = as_dict(previous_snapshot.get("map"))
    current_map = as_dict(current_snapshot.get("map"))
    previous_round = as_dict(previous_snapshot.get("round"))
    current_round = as_dict(current_snapshot.get("round"))
    previous_bomb = as_dict(previous_snapshot.get("bomb"))
    current_bomb = as_dict(current_snapshot.get("bomb"))

    prev_map_phase = previous_map.get("phase")
    curr_map_phase = current_map.get("phase")
    if curr_map_phase and prev_map_phase and curr_map_phase != prev_map_phase:
        events.append(f"map phase changed: {prev_map_phase} -> {curr_map_phase}")

    prev_round_phase = previous_round.get("phase")
    curr_round_phase = current_round.get("phase")
    if curr_round_phase and prev_round_phase and curr_round_phase != prev_round_phase:
        events.append(f"round phase changed: {prev_round_phase} -> {curr_round_phase}")

    score_event = detect_score_event(previous_map, current_map)
    if score_event:
        events.append(score_event)

    prev_bomb_state = previous_bomb.get("state")
    curr_bomb_state = current_bomb.get("state")
    if curr_bomb_state and prev_bomb_state and curr_bomb_state != prev_bomb_state:
        events.append(f"bomb state changed: {prev_bomb_state} -> {curr_bomb_state}")

    previous_player = extract_player(previous_snapshot)
    current_player = extract_player(current_snapshot)
    prev_player_state = as_dict(previous_player.get("state"))
    curr_player_state = as_dict(current_player.get("state"))
    prev_player_stats = as_dict(previous_player.get("match_stats"))
    curr_player_stats = as_dict(current_player.get("match_stats"))

    player_label = player_name(current_player, "player")

    prev_health = as_int(prev_player_state.get("health"))
    curr_health = as_int(curr_player_state.get("health"))
    if prev_health is not None and curr_health is not None:
        if curr_health <= 0 < prev_health:
            events.append(f"{player_label} has been eliminated")
        elif curr_health < prev_health - 20:
            events.append(f"{player_label} took heavy damage: {prev_health} -> {curr_health}")
        elif curr_health <= 25 < prev_health:
            events.append(f"{player_label} is low: {curr_health} HP")

    for field, label in (("flashed", "flashed"), ("burning", "burning"), ("smoked", "in smoke")):
        prev_value = as_float(prev_player_state.get(field)) or 0.0
        curr_value = as_float(curr_player_state.get(field)) or 0.0
        if curr_value > 0 and prev_value <= 0:
            events.append(f"{player_label} is {label}")

    prev_round_kills = as_int(prev_player_state.get("round_kills")) or 0
    curr_round_kills = as_int(curr_player_state.get("round_kills")) or 0
    if curr_round_kills > prev_round_kills:
        events.append(f"{player_label} now has {curr_round_kills} round kill(s)")

    prev_kills = as_int(prev_player_stats.get("kills")) or 0
    curr_kills = as_int(curr_player_stats.get("kills")) or 0
    if curr_kills > prev_kills:
        events.append(f"{player_label} added {curr_kills - prev_kills} kill(s)")

    prev_active = active_weapon_name(previous_player.get("weapons"))
    curr_active = active_weapon_name(current_player.get("weapons"))
    if curr_active and prev_active and curr_active != prev_active:
        events.append(f"{player_label} switched to {curr_active}")

    skip_names = {player_label} if current_player else set()
    events.extend(
        detect_allplayer_events(
            extract_allplayers(previous_snapshot),
            extract_allplayers(current_snapshot),
            skip_names=skip_names,
        )
    )

    unique_events = []
    seen = set()
    for event in events:
        if event not in seen:
            unique_events.append(event)
            seen.add(event)

    return unique_events[:8]


def build_play_by_play_prompt(llm_payload):
    return (
        "Call the live action as the play-by-play caster. Lead with the freshest event "
        "and make it sound immediate. One very short line only.\n\n"
        + json.dumps(llm_payload, indent=2, sort_keys=True)
    )


def build_color_prompt(llm_payload):
    prompt = (
        "Give one short color-commentary follow-up line. Focus on why the moment matters, "
        "the tactical consequence, economy pressure, or momentum. Do not repeat the "
        "play-by-play wording.\n\n"
    )
    play_by_play_line = LAST_LINES_BY_VOICE.get(VOICE_NAME, "")
    if play_by_play_line:
        prompt += f'Latest play-by-play line: "{play_by_play_line}"\n\n'
    prompt += json.dumps(llm_payload, indent=2, sort_keys=True)
    return prompt


def build_quiet_color_prompt(llm_payload, recent_booth_context, co_caster_line):
    prompt = (
        "The round is in a quieter stretch. As the color commentator, give one short live-broadcast "
        "line that fills dead air with a useful read, tactical note, player tendency, economy point, "
        "or a light reaction to your co-caster. Keep it natural and concise.\n\n"
    )
    if co_caster_line:
        prompt += f'Latest co-caster line: "{co_caster_line}"\n\n'
    if recent_booth_context:
        prompt += "Recent booth context:\n" + recent_booth_context + "\n\n"
    prompt += json.dumps(llm_payload, indent=2, sort_keys=True)
    return prompt


def build_quiet_play_by_play_prompt(llm_payload, recent_booth_context, co_caster_line):
    prompt = (
        "The round is still calm. As the play-by-play caster, answer your co-caster with one short "
        "conversational line that keeps the broadcast alive, sets the scene, or nudges the desk "
        "back toward the next point of tension.\n\n"
    )
    if co_caster_line:
        prompt += f'Latest co-caster line: "{co_caster_line}"\n\n'
    if recent_booth_context:
        prompt += "Recent booth context:\n" + recent_booth_context + "\n\n"
    prompt += json.dumps(llm_payload, indent=2, sort_keys=True)
    return prompt


def role_for_voice(voice_name):
    if voice_name == VOICE_NAME:
        return "play_by_play"
    if voice_name == SECONDARY_VOICE_NAME:
        return "color"
    return "commentator"


def recent_booth_context():
    if not RECENT_BOOTH_LINES:
        return ""

    rendered = []
    for entry in RECENT_BOOTH_LINES[-4:]:
        rendered.append(f"{entry['voice']} ({entry['role']}): {entry['text']}")
    return "\n".join(rendered)


def latest_line_from_other_voice(voice_name):
    for entry in reversed(RECENT_BOOTH_LINES):
        if entry["voice"] != voice_name:
            return entry["text"]
    return ""


def remember_booth_line(voice_name, text):
    role = role_for_voice(voice_name)
    LAST_LINES_BY_VOICE[voice_name] = text
    RECENT_BOOTH_LINES.append({"voice": voice_name, "role": role, "text": text})
    del RECENT_BOOTH_LINES[:-4]


def resolve_voice_config_file(voice_name=None):
    if VOICE_CONFIG_FILE and (voice_name in (None, "", VOICE_NAME)):
        return Path(VOICE_CONFIG_FILE).expanduser().resolve()

    manifest_path = Path(VOICE_MANIFEST_FILE).expanduser().resolve()
    if not manifest_path.is_file():
        raise RuntimeError(f"missing voice manifest: {manifest_path}")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    voices = manifest.get("voices") or []

    target_voice = voice_name or VOICE_NAME
    if target_voice:
        for voice in voices:
            if voice.get("name") == target_voice:
                env_file = voice.get("env_file")
                if env_file:
                    return Path(env_file).expanduser().resolve()
        raise RuntimeError(f"voice '{target_voice}' not found in: {manifest_path}")

    default_env_file = manifest.get("default_env_file")
    if not default_env_file:
        raise RuntimeError(f"no default voice configured in: {manifest_path}")

    return Path(default_env_file).expanduser().resolve()


def resolve_voice_runtime_config(voice_name=None):
    voice_env_path = resolve_voice_config_file(voice_name=voice_name)
    if not voice_env_path.is_file():
        raise RuntimeError(f"missing voice config file: {voice_env_path}")

    voice_env = parse_env_file(str(voice_env_path))
    config = {
        "voice_env_path": voice_env_path,
        "voice_name": voice_env.get("CUSTOM_VOICE_NAME") or voice_name or VOICE_NAME,
        "embedding_file": None,
    }

    embedding_file = voice_env.get("CUSTOM_VOICE_EMBEDDING_FILE")
    if embedding_file:
        embedding_path = Path(embedding_file).expanduser().resolve()
        if not embedding_path.is_file():
            raise RuntimeError(f"missing speaker embedding file: {embedding_path}")
        config["embedding_file"] = embedding_path

    return config


def choose_simple_dual_voice_name():
    global LAST_SELECTED_VOICE

    if VOICE_SELECTION_MODE != "dual" or not SECONDARY_VOICE_NAME:
        LAST_SELECTED_VOICE = VOICE_NAME
        return VOICE_NAME

    if DUAL_VOICE_HEURISTIC == "flip_flop":
        if LAST_SELECTED_VOICE == VOICE_NAME:
            LAST_SELECTED_VOICE = SECONDARY_VOICE_NAME
        else:
            LAST_SELECTED_VOICE = VOICE_NAME
        return LAST_SELECTED_VOICE

    if DUAL_VOICE_HEURISTIC == "random":
        if random.random() < max(0.0, min(1.0, SECONDARY_VOICE_PROBABILITY)):
            LAST_SELECTED_VOICE = SECONDARY_VOICE_NAME
        else:
            LAST_SELECTED_VOICE = VOICE_NAME
        return LAST_SELECTED_VOICE

    LAST_SELECTED_VOICE = VOICE_NAME
    return VOICE_NAME


def is_casting_roles_mode():
    return DUAL_VOICE_HEURISTIC in {"casting_roles", "casting_chemistry"}


def is_spike_event(events):
    text = " ".join(events).lower()
    spike_markers = (
        "kill",
        "goes down",
        "eliminated",
        "bomb",
        "score update",
        "heavy damage",
        "low:",
        "round phase changed",
    )
    return any(marker in text for marker in spike_markers)


def make_job(
    kind,
    voice_name,
    llm_payload,
    due_at=None,
    required_event_sequence=None,
    required_idle_seconds=None,
):
    return {
        "kind": kind,
        "voice_name": voice_name,
        "llm_payload": llm_payload,
        "due_at": due_at or time.time(),
        "required_event_sequence": required_event_sequence,
        "required_idle_seconds": required_idle_seconds,
    }


def role_for_job(job):
    if job["kind"] in {"play_by_play", "quiet_play_by_play"}:
        return "play_by_play"
    if job["kind"] in {"color_followup", "quiet_color"}:
        return "color"
    return "commentator"


def system_prompt_for_job(job):
    if job["kind"] == "play_by_play":
        return PLAY_BY_PLAY_SYSTEM_PROMPT
    if job["kind"] == "color_followup":
        return COLOR_SYSTEM_PROMPT
    if job["kind"] in {"quiet_play_by_play", "quiet_color"}:
        return CHEMISTRY_SYSTEM_PROMPT
    return SYSTEM_PROMPT


def prompt_for_job(job):
    llm_payload = job["llm_payload"]
    if job["kind"] == "play_by_play":
        return build_play_by_play_prompt(llm_payload)
    if job["kind"] == "color_followup":
        return build_color_prompt(llm_payload)
    if job["kind"] == "quiet_play_by_play":
        return build_quiet_play_by_play_prompt(
            llm_payload,
            recent_booth_context(),
            latest_line_from_other_voice(job["voice_name"]),
        )

    return build_quiet_color_prompt(
        llm_payload,
        recent_booth_context(),
        latest_line_from_other_voice(job["voice_name"]),
    )


def job_is_stale(job):
    required_event_sequence = job.get("required_event_sequence")
    required_idle_seconds = job.get("required_idle_seconds")

    with STATE_LOCK:
        current_event_sequence = EVENT_SEQUENCE
        last_event_at = LAST_EVENT_AT

    if (
        required_event_sequence is not None
        and current_event_sequence != required_event_sequence
    ):
        return True

    if required_idle_seconds is not None:
        if time.time() - last_event_at < required_idle_seconds:
            return True

    return False


def enqueue_job(job):
    try:
        COMMENTARY_QUEUE.put_nowait((job["due_at"], next_job_order(), job))
        return True
    except queue.Full:
        return False


def next_job_order():
    global JOB_COUNTER

    order = JOB_COUNTER
    JOB_COUNTER += 1
    return order


def build_payload_for_logging(job):
    return prune_empty(
        {
            "kind": job["kind"],
            "voice": job["voice_name"],
            "role": role_for_job(job),
            "state": job["llm_payload"].get("state"),
            "events": job["llm_payload"].get("events"),
            "timestamp": job["llm_payload"].get("timestamp"),
        }
    )


def iter_sse_content(response):
    while True:
        raw_line = response.readline()
        if not raw_line:
            break

        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line or not line.startswith("data: "):
            continue

        payload = line[6:]
        if payload == "[DONE]":
            break

        event = json.loads(payload)
        choices = event.get("choices") or []
        if not choices:
            continue

        delta = as_dict(choices[0].get("delta"))
        content = delta.get("content")
        if isinstance(content, str) and content:
            yield content


def stream_commentary_to_tts(prompt_text, voice_runtime_config, system_prompt):
    request_body = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": True,
    }

    request = urllib.request.Request(
        f"{MODEL_API_BASE}/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    tts_cmd = [
        TTS_PYTHON,
        TTS_STREAM_SCRIPT,
        "--url",
        SERVER_URL,
        "--stdin-chunks",
    ]
    if tts_workspace_name() == "tts-io-full":
        tts_cmd.extend(["--voice-name", str(voice_runtime_config["voice_name"])])
    else:
        speaker_embedding_file = voice_runtime_config.get("embedding_file")
        if speaker_embedding_file is None:
            raise RuntimeError("No speaker embedding file was resolved for tts-io.")
        tts_cmd.extend(["--speaker-embedding-file", str(speaker_embedding_file)])
    tts_proc = None
    commentary_chunks = []
    pending_error = None

    try:
        tts_proc = subprocess.Popen(
            tts_cmd,
            stdin=subprocess.PIPE,
            cwd=str(REPO_ROOT),
        )

        if tts_proc.stdin is None:
            raise RuntimeError("failed to open stdin for TTS process")

        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            for content in iter_sse_content(response):
                commentary_chunks.append(content)
                print(content, end="", flush=True)
                tts_proc.stdin.write(content.encode("utf-8"))
                tts_proc.stdin.write(b"\n")
                tts_proc.stdin.flush()

        print("", flush=True)
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        pending_error = RuntimeError(f"HTTP {error.code}: {body}")
    except urllib.error.URLError as error:
        pending_error = RuntimeError(f"model request failed: {error}")
    except Exception as error:
        pending_error = error
    finally:
        if tts_proc and tts_proc.stdin:
            tts_proc.stdin.close()
        if tts_proc:
            return_code = tts_proc.wait()
            if return_code != 0 and pending_error is None:
                pending_error = RuntimeError(f"TTS stream exited with status {return_code}")

    if pending_error is not None:
        raise pending_error

    final_text = "".join(commentary_chunks).strip()
    if not final_text:
        raise RuntimeError("model returned no commentary text")

    return final_text


def maybe_schedule_simple_commentary(llm_payload, events):
    global LAST_COMMENTARY_AT, LAST_PROMPT_HASH

    if not ENABLE_COMMENTARY:
        return False

    if COMMENTARY_ONLY_ON_EVENTS and not events:
        return False

    selected_voice = choose_simple_dual_voice_name()
    job = make_job("play_by_play", selected_voice, llm_payload)
    job_hash = hashlib.sha1(
        json.dumps(
            {
                "kind": job["kind"],
                "voice": selected_voice,
                "payload": llm_payload,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    now = time.time()

    with STATE_LOCK:
        if job_hash == LAST_PROMPT_HASH:
            return False
        if now - LAST_COMMENTARY_AT < COMMENTARY_MIN_INTERVAL:
            return False
        LAST_PROMPT_HASH = job_hash

    return enqueue_job(job)


def maybe_schedule_role_commentary(llm_payload, events):
    global LAST_PROMPT_HASH

    if not ENABLE_COMMENTARY or not events:
        return False

    job_hash = hashlib.sha1(
        json.dumps(
            {
                "kind": "play_by_play",
                "voice": VOICE_NAME,
                "payload": llm_payload,
            },
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    now = time.time()

    with STATE_LOCK:
        if job_hash == LAST_PROMPT_HASH:
            return False
        if now - LAST_COMMENTARY_AT < COMMENTARY_MIN_INTERVAL:
            return False
        LAST_PROMPT_HASH = job_hash
        event_sequence = EVENT_SEQUENCE

    scheduled = enqueue_job(make_job("play_by_play", VOICE_NAME, llm_payload))

    if (
        scheduled
        and SECONDARY_VOICE_NAME
        and is_spike_event(events)
    ):
        color_due_at = time.time() + max(COLOR_FOLLOWUP_DELAY, COMMENTARY_MIN_INTERVAL)
        enqueue_job(
            make_job(
                "color_followup",
                SECONDARY_VOICE_NAME,
                llm_payload,
                due_at=color_due_at,
                required_event_sequence=event_sequence,
            )
        )

    return scheduled


def choose_quiet_voice_name():
    global LAST_QUIET_SPEAKER

    if DUAL_VOICE_HEURISTIC == "casting_roles" or not SECONDARY_VOICE_NAME:
        LAST_QUIET_SPEAKER = SECONDARY_VOICE_NAME
        return SECONDARY_VOICE_NAME, "quiet_color"

    if LAST_QUIET_SPEAKER in (None, VOICE_NAME):
        LAST_QUIET_SPEAKER = SECONDARY_VOICE_NAME
        return SECONDARY_VOICE_NAME, "quiet_color"

    LAST_QUIET_SPEAKER = VOICE_NAME
    return VOICE_NAME, "quiet_play_by_play"


def maybe_schedule_commentary(llm_payload, events):
    if not ENABLE_COMMENTARY:
        return False

    if VOICE_SELECTION_MODE != "dual":
        return maybe_schedule_simple_commentary(llm_payload, events)

    if DUAL_VOICE_HEURISTIC in {"random", "flip_flop"}:
        return maybe_schedule_simple_commentary(llm_payload, events)

    return maybe_schedule_role_commentary(llm_payload, events)


def chemistry_scheduler_worker():
    while True:
        time.sleep(0.5)

        if not (
            ENABLE_COMMENTARY
            and VOICE_SELECTION_MODE == "dual"
            and DUAL_VOICE_HEURISTIC in {"casting_roles", "casting_chemistry"}
            and SECONDARY_VOICE_NAME
        ):
            continue

        with STATE_LOCK:
            snapshot = copy.deepcopy(LATEST_SNAPSHOT)
            event_sequence = EVENT_SEQUENCE
            last_event_at = LAST_EVENT_AT
            last_commentary_at = LAST_COMMENTARY_AT
            last_quiet_commentary_at = LAST_QUIET_COMMENTARY_AT

        if not snapshot:
            continue

        now = time.time()
        if last_event_at <= 0:
            continue
        if now - last_event_at < QUIET_IDLE_SECONDS:
            continue
        if now - last_commentary_at < QUIET_COMMENTARY_INTERVAL:
            continue
        if now - last_quiet_commentary_at < QUIET_COMMENTARY_INTERVAL:
            continue

        llm_payload = build_llm_payload(snapshot, [])
        quiet_voice_name, job_kind = choose_quiet_voice_name()
        enqueue_job(
            make_job(
                job_kind,
                quiet_voice_name,
                llm_payload,
                required_event_sequence=event_sequence,
                required_idle_seconds=QUIET_IDLE_SECONDS,
            )
        )


def commentary_worker():
    global LAST_COMMENTARY_AT, LAST_QUIET_COMMENTARY_AT

    while True:
        try:
            due_at, _, job = COMMENTARY_QUEUE.get(timeout=0.5)
        except queue.Empty:
            continue

        try:
            delay = due_at - time.time()
            if delay > 0:
                COMMENTARY_QUEUE.put_nowait((due_at, next_job_order(), job))
                time.sleep(min(delay, 0.25))
                continue

            if job_is_stale(job):
                continue

            selected_voice = job["voice_name"]
            voice_runtime_config = SPEAKER_EMBEDDING_FILES[selected_voice]
            system_prompt = system_prompt_for_job(job)
            prompt_text = prompt_for_job(job)
            payload_for_logging = build_payload_for_logging(job)

            with STATE_LOCK:
                LAST_COMMENTARY_AT = time.time()
                if job["kind"] in {"quiet_play_by_play", "quiet_color"}:
                    LAST_QUIET_COMMENTARY_AT = LAST_COMMENTARY_AT

            print("=== Commentary Payload ===", flush=True)
            print(json.dumps(payload_for_logging, indent=2, sort_keys=True), flush=True)
            print("=== Commentary Stream ===", flush=True)
            print(f"Voice: {selected_voice}", flush=True)
            print(f"Role: {role_for_job(job)}", flush=True)
            commentary = stream_commentary_to_tts(
                prompt_text,
                voice_runtime_config,
                system_prompt,
            )
            remember_booth_line(selected_voice, commentary)
            print("=== Commentary Complete ===", flush=True)
            print(commentary, flush=True)
        except Exception as error:
            print(f"Commentary request failed: {error}", flush=True)
        finally:
            COMMENTARY_QUEUE.task_done()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global EVENT_SEQUENCE, LAST_EVENT_AT, LAST_QUIET_SPEAKER, LATEST_SNAPSHOT

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
            previous_snapshot = copy.deepcopy(LATEST_SNAPSHOT)
            LATEST_SNAPSHOT = deep_merge(LATEST_SNAPSHOT, payload)
            current_snapshot = copy.deepcopy(LATEST_SNAPSHOT)
            if LAST_EVENT_AT <= 0:
                LAST_EVENT_AT = time.time()

        events = detect_events(previous_snapshot, current_snapshot)
        llm_payload = build_llm_payload(current_snapshot, events)
        if events:
            with STATE_LOCK:
                EVENT_SEQUENCE += 1
                LAST_EVENT_AT = time.time()
                LAST_QUIET_SPEAKER = None

        if maybe_schedule_commentary(llm_payload, events):
            print("Commentary request queued.", flush=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def main():
    global SPEAKER_EMBEDDING_FILES

    if ENABLE_COMMENTARY:
        SPEAKER_EMBEDDING_FILES[VOICE_NAME] = resolve_voice_runtime_config(VOICE_NAME)
        if VOICE_SELECTION_MODE == "dual" and SECONDARY_VOICE_NAME:
            SPEAKER_EMBEDDING_FILES[SECONDARY_VOICE_NAME] = resolve_voice_runtime_config(
                SECONDARY_VOICE_NAME
            )
        worker = threading.Thread(target=commentary_worker, daemon=True)
        worker.start()
        chemistry_worker = threading.Thread(target=chemistry_scheduler_worker, daemon=True)
        chemistry_worker.start()

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Commentary enabled: {'yes' if ENABLE_COMMENTARY else 'no'}", flush=True)
    print(f"Model endpoint: {MODEL_API_BASE}/v1/chat/completions", flush=True)
    if ENABLE_COMMENTARY:
        print(f"TTS endpoint: {SERVER_URL}", flush=True)
        print(f"TTS workspace: {tts_workspace_name()}", flush=True)
        print(f"Voice mode: {VOICE_SELECTION_MODE}", flush=True)
        print(f"Primary voice: {VOICE_NAME}", flush=True)
        print(
            f"Primary voice runtime: {SPEAKER_EMBEDDING_FILES.get(VOICE_NAME)}",
            flush=True,
        )
        if VOICE_SELECTION_MODE == "dual" and SECONDARY_VOICE_NAME:
            print(f"Secondary voice: {SECONDARY_VOICE_NAME}", flush=True)
            print(f"Dual voice heuristic: {DUAL_VOICE_HEURISTIC}", flush=True)
            print(
                "Secondary voice runtime: "
                f"{SPEAKER_EMBEDDING_FILES.get(SECONDARY_VOICE_NAME)}",
                flush=True,
            )
            print(
                "Secondary voice probability: "
                f"{max(0.0, min(1.0, SECONDARY_VOICE_PROBABILITY))}",
                flush=True,
            )
            if DUAL_VOICE_HEURISTIC in {"casting_roles", "casting_chemistry"}:
                print(
                    f"Color follow-up delay: {max(COLOR_FOLLOWUP_DELAY, COMMENTARY_MIN_INTERVAL)}",
                    flush=True,
                )
                print(f"Quiet idle seconds: {QUIET_IDLE_SECONDS}", flush=True)
                print(f"Quiet commentary interval: {QUIET_COMMENTARY_INTERVAL}", flush=True)

    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
