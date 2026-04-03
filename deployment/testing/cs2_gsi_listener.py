import copy
import hashlib
import json
import os
import queue
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


EXPECTED_TOKEN = env_text("CS2_GSI_AUTH_TOKEN", "") or None
HOST = env_text("CS2_GSI_HOST", "127.0.0.1")
PORT = env_int("CS2_GSI_PORT", 3000)

PRINT_RAW = env_bool("CS2_GSI_PRINT_RAW", False)
PRINT_SUMMARY = env_bool("CS2_GSI_PRINT_SUMMARY", True)
PRINT_LLM_PAYLOAD = env_bool("CS2_GSI_PRINT_LLM_PAYLOAD", True)
ENABLE_COMMENTARY = env_bool("CS2_GSI_ENABLE_COMMENTARY", False)
COMMENTARY_ONLY_ON_EVENTS = env_bool("CS2_GSI_COMMENTARY_ONLY_ON_EVENTS", True)
COMMENTARY_MIN_INTERVAL = env_float("CS2_GSI_COMMENTARY_MIN_INTERVAL", 2.0)
MODEL_TIMEOUT = env_float("CS2_GSI_MODEL_TIMEOUT", 4.0)

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
TEMPERATURE = env_float("TEMPERATURE", 0.4)
MAX_TOKENS = env_int("MAX_TOKENS", 100)

STATE_LOCK = threading.Lock()
LATEST_SNAPSHOT = {}
LAST_PROMPT_HASH = None
LAST_COMMENTARY_AT = 0.0
COMMENTARY_QUEUE = queue.Queue(maxsize=4)


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


def build_commentary_prompt(llm_payload):
    return (
        "Generate live Counter-Strike 2 commentary from this structured state update. "
        "Focus on the freshest event when one exists, otherwise call the current tension.\n\n"
        + json.dumps(llm_payload, indent=2, sort_keys=True)
    )


def request_commentary(prompt_text):
    request_body = {
        "model": MODEL_NAME,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt_text},
        ],
        "temperature": TEMPERATURE,
        "max_tokens": MAX_TOKENS,
        "stream": False,
    }

    request = urllib.request.Request(
        f"{MODEL_API_BASE}/v1/chat/completions",
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=MODEL_TIMEOUT) as response:
            raw_body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {error.code}: {body}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"model request failed: {error}") from error

    response_json = json.loads(raw_body)
    choices = response_json.get("choices") or []
    if not choices:
        raise RuntimeError(f"unexpected model response: {raw_body}")

    message = as_dict(choices[0].get("message"))
    content = message.get("content")
    if not isinstance(content, str):
        raise RuntimeError(f"missing response content: {raw_body}")

    return content.strip()


def maybe_enqueue_commentary(prompt_text, events):
    global LAST_COMMENTARY_AT, LAST_PROMPT_HASH

    if not ENABLE_COMMENTARY:
        return False

    if COMMENTARY_ONLY_ON_EVENTS and not events:
        return False

    prompt_hash = hashlib.sha1(prompt_text.encode("utf-8")).hexdigest()
    now = time.time()

    with STATE_LOCK:
        if prompt_hash == LAST_PROMPT_HASH:
            return False
        if now - LAST_COMMENTARY_AT < COMMENTARY_MIN_INTERVAL:
            return False

        LAST_PROMPT_HASH = prompt_hash
        LAST_COMMENTARY_AT = now

    try:
        COMMENTARY_QUEUE.put_nowait(prompt_text)
        return True
    except queue.Full:
        return False


def commentary_worker():
    while True:
        prompt_text = COMMENTARY_QUEUE.get()
        try:
            commentary = request_commentary(prompt_text)
            print("=== Commentary Output ===", flush=True)
            print(commentary, flush=True)
        except Exception as error:
            print(f"Commentary request failed: {error}", flush=True)
        finally:
            COMMENTARY_QUEUE.task_done()


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global LATEST_SNAPSHOT

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

        events = detect_events(previous_snapshot, current_snapshot)
        llm_payload = build_llm_payload(current_snapshot, events)
        prompt_text = build_commentary_prompt(llm_payload)

        if PRINT_RAW:
            print("=== Raw GSI JSON ===", flush=True)
            print(json.dumps(payload, indent=2, sort_keys=True), flush=True)

        if PRINT_SUMMARY:
            print(render_summary(llm_payload), flush=True)

        if PRINT_LLM_PAYLOAD:
            print("=== Commentary Payload ===", flush=True)
            print(json.dumps(llm_payload, indent=2, sort_keys=True), flush=True)

        if maybe_enqueue_commentary(prompt_text, events):
            print("Commentary request queued.", flush=True)

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


def main():
    if ENABLE_COMMENTARY:
        worker = threading.Thread(target=commentary_worker, daemon=True)
        worker.start()

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Commentary enabled: {'yes' if ENABLE_COMMENTARY else 'no'}", flush=True)
    print(f"Model endpoint: {MODEL_API_BASE}/v1/chat/completions", flush=True)

    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
