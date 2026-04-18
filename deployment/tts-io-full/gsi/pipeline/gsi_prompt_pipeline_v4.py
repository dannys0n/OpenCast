import copy
import json
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gsi_prompt_pipeline_v2 import (
    EXPECTED_TOKEN,
    HOST,
    KILL_EXISTING_LISTENER,
    PORT,
    as_dict,
    compute_score,
    filter_important_events,
    normalize_player,
    normalize_team,
)
from prompt_queue_v4 import (
    load_prompt_config as load_prompt_runtime_config,
    next_idle_mode,
    process_event_wrapper,
    process_interval_wrapper,
    reset_prompt_runtime_state,
    slim_log,
)


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state" / "v4"
RAW_GSI_PATH = STATE_DIR / "gsi_received_pretty.jsonl"
RAW_GSI_LATEST_PATH = STATE_DIR / "gsi_received_latest.json"
FILTERED_EVENTS_PATH = STATE_DIR / "gsi_filtered_pretty.jsonl"
FILTERED_EVENTS_LATEST_PATH = STATE_DIR / "gsi_filtered_latest.json"
PROMPT_WRAPPER_PATH = STATE_DIR / "prompt_wrapper_pretty.jsonl"
PROMPT_WRAPPER_LATEST_PATH = STATE_DIR / "prompt_wrapper_latest.json"
PIPELINE_LOG = STATE_DIR / "pipeline_v4.log"

STATE_LOCK = threading.Lock()


@dataclass
class PipelineState:
    latest_snapshot: dict | None = None
    previous_snapshot: dict | None = None
    payload_count: int = 0
    last_event_prompt_at: float = 0.0
    last_interval_prompt_at: float = 0.0
    event_analysis_toggle: int = 0


PIPELINE_STATE = PipelineState()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        RAW_GSI_PATH,
        RAW_GSI_LATEST_PATH,
        FILTERED_EVENTS_PATH,
        FILTERED_EVENTS_LATEST_PATH,
        PROMPT_WRAPPER_PATH,
        PROMPT_WRAPPER_LATEST_PATH,
        PIPELINE_LOG,
    ]:
        path.touch(exist_ok=True)


def append_log(text):
    ensure_state_dir()
    with PIPELINE_LOG.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


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
        PROMPT_WRAPPER_PATH,
        PROMPT_WRAPPER_LATEST_PATH,
        PIPELINE_LOG,
    ]:
        path.write_text("", encoding="utf-8")


def build_alive_players(snapshot):
    snapshot = as_dict(snapshot)
    map_name = as_dict(snapshot.get("map")).get("name")
    alive_players = []

    allplayers = as_dict(snapshot.get("allplayers"))
    if allplayers:
        for entity_id, player in allplayers.items():
            normalized = normalize_player(entity_id, player, map_name=map_name)
            if not normalized:
                continue
            if int(normalized.get("health") or 0) <= 0:
                continue
            alive_players.append(
                {
                    "name": normalized.get("name"),
                    "team": normalized.get("team"),
                    "map_callout": normalized.get("map_callout"),
                }
            )
    else:
        local_player = normalize_player(None, snapshot.get("player"), map_name=map_name)
        if local_player and int(local_player.get("health") or 0) > 0:
            alive_players.append(
                {
                    "name": local_player.get("name"),
                    "team": local_player.get("team"),
                    "map_callout": local_player.get("map_callout"),
                }
            )

    alive_players = [player for player in alive_players if player.get("name")]
    alive_players.sort(key=lambda player: (str(player.get("team") or ""), str(player.get("name") or "")))
    return alive_players


def build_match_context(snapshot):
    snapshot = as_dict(snapshot)
    map_data = as_dict(snapshot.get("map"))
    round_data = as_dict(snapshot.get("round"))
    score = compute_score(snapshot)

    return {
        "map_name": map_data.get("name"),
        "map_phase": map_data.get("phase"),
        "round_phase": round_data.get("phase"),
        "round_number": map_data.get("round"),
        "score": {
            "CT": score["ct"],
            "T": score["t"],
        },
        "win_team": normalize_team(round_data.get("win_team")),
        "alive_players": build_alive_players(snapshot),
    }


def build_event_analysis_caster():
    with STATE_LOCK:
        caster = "caster0" if PIPELINE_STATE.event_analysis_toggle % 2 == 0 else "caster1"
        PIPELINE_STATE.event_analysis_toggle += 1
        return caster


def build_request(mode, *, event_analysis_caster=None):
    if mode == "event_trigger":
        return {
            "mode": "event_trigger",
            "output_count": 2,
        }

    if mode == "chemistry":
        return {
            "mode": "chemistry",
            "output_count": 3,
        }

    return {
        "mode": "idle_analysis",
        "output_count": 3,
    }


def describe_kill_event(event):
    event = as_dict(event)
    killer = as_dict(event.get("killer"))
    victim = as_dict(event.get("victim"))
    player = as_dict(event.get("player"))
    kill_count = killer.get("round_kills")
    if kill_count in (None, ""):
        kill_count = event.get("kill_count")

    try:
        kill_count = int(kill_count)
    except (TypeError, ValueError):
        kill_count = None

    killer_name = killer.get("name") or player.get("name")
    victim_name = victim.get("name")

    if killer_name and kill_count == 5:
        return f"Ace for {killer_name}"
    if killer_name and kill_count in {2, 3, 4}:
        return f"{kill_count} kills for {killer_name}"
    if killer_name and victim_name:
        return f"{killer_name} kills {victim_name}"
    if killer_name:
        return f"Kill for {killer_name}"
    return "Kill"


def describe_event(event):
    event = as_dict(event)
    event_type = event.get("event_type")

    if event_type in {"kill", "player_scored_kill"}:
        return describe_kill_event(event)
    if event_type == "kill_cluster":
        killers = event.get("killers") or []
        first_killer = as_dict(killers[0]) if killers else {}
        killer_name = first_killer.get("name")
        try:
            kill_count = int(event.get("kill_count"))
        except (TypeError, ValueError):
            kill_count = None
        if killer_name and kill_count == 5:
            return f"Ace for {killer_name}"
        if killer_name and kill_count in {2, 3, 4}:
            return f"{kill_count} kills for {killer_name}"
        if killer_name and kill_count:
            return f"{kill_count} kills in the fight for {killer_name}"
        return "Kill cluster"
    if event_type == "player_death":
        player = as_dict(event.get("player"))
        if player.get("name"):
            return f"{player.get('name')} goes down"
        return "Player dies"
    if event_type == "grenade_detonated":
        owner = as_dict(event.get("owner_player"))
        grenade_type = event.get("grenade_type")
        callout = event.get("detonation_callout")
        parts = []
        if owner.get("name"):
            parts.append(owner.get("name"))
        if grenade_type:
            parts.append(str(grenade_type))
        if callout:
            parts.append(f"at {callout}")
        return " ".join(parts) if parts else "Grenade detonates"
    if event_type == "grenade_thrown":
        owner = as_dict(event.get("owner_player"))
        grenade_type = event.get("grenade_type")
        if owner.get("name") and grenade_type:
            return f"{owner.get('name')} throws {grenade_type}"
        if grenade_type:
            return f"{grenade_type} thrown"
        return "Grenade thrown"
    if event_type == "bomb_event":
        state_after = event.get("state_after")
        if state_after:
            return f"Bomb {state_after}"
        return "Bomb event"
    if event_type == "round_result":
        winner = event.get("winner")
        if winner:
            return f"{winner} win the round"
        return "Round ends"
    if event_type == "game_over":
        winner = event.get("winner")
        if winner:
            return f"{winner} win the map"
        return "Game over"
    if event_type == "team_counter":
        alive_counts = as_dict(event.get("alive_counts_after"))
        ct = alive_counts.get("CT")
        t_value = alive_counts.get("T")
        if ct is not None and t_value is not None:
            return f"{ct} vs {t_value} alive"
        return "Alive count changes"
    return str(event_type or "event").replace("_", " ")


def build_event_descriptions(events):
    descriptions = []
    for event in events:
        description = " ".join(describe_event(event).split()).strip()
        if description:
            descriptions.append(description)
    return descriptions


def build_event_wrapper(filtered_batch):
    events = copy.deepcopy(as_dict(filtered_batch).get("events", []))
    analysis_caster = build_event_analysis_caster()
    return {
        "input": {
            "event_descriptions": build_event_descriptions(events),
            "current_events": events,
            "request": build_request("event_trigger", event_analysis_caster=analysis_caster),
            "analysis_caster": analysis_caster,
        }
    }


def build_idle_wrapper(current_snapshot, mode):
    match_context = build_match_context(current_snapshot)
    return {
        "input": {
            "score": copy.deepcopy(match_context.get("score")),
            "player_locations": copy.deepcopy(match_context.get("alive_players")),
            "request": build_request(mode),
        }
    }


def should_log_prompt_wrapper(mode):
    return mode != "chemistry"


def repo_root():
    return SCRIPT_DIR.parents[3]


def start_background_prompt_thread(target, *args, **kwargs):
    thread = threading.Thread(target=target, args=args, kwargs=kwargs, daemon=True)
    thread.start()
    return thread


def interval_seconds():
    config = load_prompt_runtime_config()
    value = config.get("interval_seconds", 10)
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 10


def interval_prompt_loop():
    while True:
        time.sleep(1.0)
        with STATE_LOCK:
            current_snapshot = copy.deepcopy(PIPELINE_STATE.latest_snapshot)
            payload_sequence = PIPELINE_STATE.payload_count
            last_event_prompt_at = PIPELINE_STATE.last_event_prompt_at
            last_interval_prompt_at = PIPELINE_STATE.last_interval_prompt_at

        if not current_snapshot:
            continue

        match_context = build_match_context(current_snapshot)
        if match_context.get("map_phase") != "live":
            continue
        if match_context.get("round_phase") != "live":
            continue

        now_ts = time.time()
        if now_ts - last_event_prompt_at < interval_seconds():
            continue
        if now_ts - last_interval_prompt_at < interval_seconds():
            continue

        interval_mode = next_idle_mode()
        prompt_wrapper = build_idle_wrapper(current_snapshot, interval_mode)
        if should_log_prompt_wrapper(interval_mode):
            append_pretty_json_record(PROMPT_WRAPPER_PATH, prompt_wrapper)
            write_pretty_json_file(PROMPT_WRAPPER_LATEST_PATH, prompt_wrapper)
        start_background_prompt_thread(
            process_interval_wrapper,
            prompt_wrapper,
            repo_root(),
            payload_sequence=payload_sequence,
            interval_mode=interval_mode,
        )
        with STATE_LOCK:
            PIPELINE_STATE.last_interval_prompt_at = time.time()


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

    slim_log("port reclaim", commentary=f"Port {port} busy; reclaiming PID(s): {', '.join(map(str, pids))}", include_commentary=True)

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
        append_pretty_json_record(RAW_GSI_PATH, raw_record)
        write_pretty_json_file(RAW_GSI_LATEST_PATH, raw_record)

        filtered_batch = filter_important_events(
            previous_snapshot,
            current_snapshot,
            payload_sequence=payload_sequence,
            payload=payload,
        )

        prompt_wrapper = None
        if filtered_batch["events"]:
            prompt_wrapper = build_event_wrapper(filtered_batch)
            append_pretty_json_record(FILTERED_EVENTS_PATH, filtered_batch)
            write_pretty_json_file(FILTERED_EVENTS_LATEST_PATH, filtered_batch)
            append_pretty_json_record(PROMPT_WRAPPER_PATH, prompt_wrapper)
            write_pretty_json_file(PROMPT_WRAPPER_LATEST_PATH, prompt_wrapper)

            with STATE_LOCK:
                PIPELINE_STATE.last_event_prompt_at = time.time()

            slim_log(
                "filtered",
                commentary=f"payload #{payload_sequence} -> {len(filtered_batch['events'])} filtered event(s) + prompt wrapper",
                include_commentary=True,
            )

        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except OSError:
            return

        if prompt_wrapper is not None:
            start_background_prompt_thread(
                process_event_wrapper,
                prompt_wrapper,
                repo_root(),
                payload_sequence=payload_sequence,
            )

    def log_message(self, format, *args):
        return


def main():
    reset_session_files()
    reset_prompt_runtime_state()
    PIPELINE_STATE.latest_snapshot = None
    PIPELINE_STATE.previous_snapshot = None
    PIPELINE_STATE.payload_count = 0
    PIPELINE_STATE.last_event_prompt_at = 0.0
    PIPELINE_STATE.last_interval_prompt_at = 0.0
    PIPELINE_STATE.event_analysis_toggle = 0
    append_log(f"[{now_stamp()}] pipeline v4 session started\n")

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    slim_log("startup", commentary=f"Listening on http://{HOST}:{PORT}", include_commentary=True)
    slim_log("startup", commentary=f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", include_commentary=True)
    slim_log("startup", commentary=f"Raw GSI history: {RAW_GSI_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Raw GSI latest: {RAW_GSI_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Filtered history: {FILTERED_EVENTS_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Filtered latest: {FILTERED_EVENTS_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Prompt wrapper history: {PROMPT_WRAPPER_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Prompt wrapper latest: {PROMPT_WRAPPER_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Pipeline log: {PIPELINE_LOG}", include_commentary=True)
    slim_log(
        "startup",
        commentary="This listener stores raw JSON, filtered events, minimal v4 wrappers, and runs event/idle/chemistry prompt experiments.",
        include_commentary=True,
    )

    start_background_prompt_thread(interval_prompt_loop)
    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
