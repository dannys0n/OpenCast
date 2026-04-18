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
    next_interval_mode,
    process_event_wrapper,
    process_interval_wrapper,
    reset_prompt_runtime_state,
    slim_log,
)
from tactical_rules_v4 import build_derived_tactical_summary


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state" / "v4"
RAW_GSI_PATH = STATE_DIR / "gsi_received_pretty.jsonl"
RAW_GSI_LATEST_PATH = STATE_DIR / "gsi_received_latest.json"
FILTERED_EVENTS_PATH = STATE_DIR / "gsi_filtered_pretty.jsonl"
FILTERED_EVENTS_LATEST_PATH = STATE_DIR / "gsi_filtered_latest.json"
TRAINING_WRAPPER_PATH = STATE_DIR / "training_wrapper_pretty.jsonl"
TRAINING_WRAPPER_LATEST_PATH = STATE_DIR / "training_wrapper_latest.json"
PIPELINE_LOG = STATE_DIR / "pipeline_v4.log"

STATE_LOCK = threading.Lock()

@dataclass
class PipelineState:
    latest_snapshot: dict | None = None
    previous_snapshot: dict | None = None
    payload_count: int = 0
    previous_event_summary: list | None = None
    last_event_prompt_at: float = 0.0
    last_interval_prompt_at: float = 0.0


PIPELINE_STATE = PipelineState()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def snapshot_map_identity(snapshot):
    snapshot = as_dict(snapshot)
    map_data = as_dict(snapshot.get("map"))
    return {
        "map_name": map_data.get("name"),
        "map_round": map_data.get("round"),
        "map_phase": map_data.get("phase"),
    }


def should_reset_for_new_session(previous_snapshot, current_snapshot):
    previous_snapshot = as_dict(previous_snapshot)
    current_snapshot = as_dict(current_snapshot)
    if not previous_snapshot or not current_snapshot:
        return False

    previous_identity = snapshot_map_identity(previous_snapshot)
    current_identity = snapshot_map_identity(current_snapshot)

    previous_name = previous_identity.get("map_name")
    current_name = current_identity.get("map_name")
    if previous_name and current_name and previous_name != current_name:
        return True

    previous_round = previous_identity.get("map_round")
    current_round = current_identity.get("map_round")
    try:
        if previous_round is not None and current_round is not None and int(current_round) < int(previous_round):
            return True
    except (TypeError, ValueError):
        pass

    previous_phase = str(previous_identity.get("map_phase") or "")
    current_phase = str(current_identity.get("map_phase") or "")
    if previous_phase in {"gameover", "intermission"} and current_phase in {"warmup", "live"}:
        return True

    return False


def reset_runtime_session_state(*, keep_current_snapshot=False):
    if not keep_current_snapshot:
        PIPELINE_STATE.previous_snapshot = None
        PIPELINE_STATE.latest_snapshot = None
    PIPELINE_STATE.previous_event_summary = []
    PIPELINE_STATE.last_event_prompt_at = 0.0
    PIPELINE_STATE.last_interval_prompt_at = 0.0


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        RAW_GSI_PATH,
        RAW_GSI_LATEST_PATH,
        FILTERED_EVENTS_PATH,
        FILTERED_EVENTS_LATEST_PATH,
        TRAINING_WRAPPER_PATH,
        TRAINING_WRAPPER_LATEST_PATH,
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
        TRAINING_WRAPPER_PATH,
        TRAINING_WRAPPER_LATEST_PATH,
        PIPELINE_LOG,
    ]:
        path.write_text("", encoding="utf-8")


def build_match_context(snapshot):
    snapshot = as_dict(snapshot)
    map_data = as_dict(snapshot.get("map"))
    round_data = as_dict(snapshot.get("round"))
    bomb_data = as_dict(snapshot.get("bomb"))
    score = compute_score(snapshot)

    return {
        "map_name": map_data.get("name"),
        "map_phase": map_data.get("phase"),
        "round_phase": round_data.get("phase"),
        "round_number": map_data.get("round"),
        "bomb_state": round_data.get("bomb") or bomb_data.get("state"),
        "score": {
            "CT": score["ct"],
            "T": score["t"],
        },
        "win_team": normalize_team(round_data.get("win_team")),
        "alive_players": build_alive_players(snapshot),
    }


def build_training_context(snapshot):
    match_context = build_match_context(snapshot)
    return {
        "bomb_state": match_context.get("bomb_state"),
        "score": copy.deepcopy(match_context.get("score")),
        "alive_players": copy.deepcopy(match_context.get("alive_players")),
    }


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


def simplify_filtered_batch_for_training(filtered_batch):
    filtered_batch = as_dict(filtered_batch)
    return copy.deepcopy(filtered_batch.get("events", []))


def build_request(mode):
    if mode == "event_bundle":
        return {
            "mode": "event_bundle",
            "lines": [
                {"caster": "caster0", "style": "play_by_play_event"},
                {"caster": "caster1", "style": "play_by_play_follow_up"},
            ],
        }

    if mode == "idle_conversation":
        return {
            "mode": "idle_conversation",
            "lines": [
                {"caster": "caster0", "style": "idle_color"},
                {"caster": "caster1", "style": "idle_color"},
                {"caster": "caster0", "style": "idle_color"},
            ],
        }

    return {
        "mode": "idle_color",
        "lines": [
            {"caster": "caster1", "style": "idle_color"},
            {"caster": "caster1", "style": "idle_color"},
            {"caster": "caster1", "style": "idle_color"},
        ],
    }


def simplify_player_for_previous_event(player):
    player = as_dict(player)
    return {
        key: value
        for key, value in {
            "name": player.get("name"),
            "team": player.get("team"),
            "map_callout": player.get("map_callout"),
        }.items()
        if value not in (None, "", [], {})
    }


def event_priority(event):
    event_type = as_dict(event).get("event_type")
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
    return priorities.get(event_type, 0)


def simplify_event_for_previous_context(event):
    event = as_dict(event)
    event_type = event.get("event_type")

    if event_type == "kill":
        simplified = {
            "event_type": "kill",
            "killer": simplify_player_for_previous_event(event.get("killer")),
            "victim": {
                key: value
                for key, value in {
                    "name": as_dict(event.get("victim")).get("name"),
                    "team": as_dict(event.get("victim")).get("team"),
                }.items()
                if value not in (None, "", [], {})
            },
        }
        return {key: value for key, value in simplified.items() if value not in (None, "", [], {})}

    if event_type in {"player_scored_kill", "player_death"}:
        return {
            "event_type": event_type,
            "player": simplify_player_for_previous_event(event.get("player")),
        }

    if event_type == "grenade_thrown":
        return {
            "event_type": "grenade_thrown",
            "grenade_type": event.get("grenade_type"),
            "owner_player": simplify_player_for_previous_event(event.get("owner_player")),
        }

    if event_type == "grenade_detonated":
        return {
            "event_type": "grenade_detonated",
            "grenade_type": event.get("grenade_type"),
            "detonation_callout": event.get("detonation_callout"),
            "owner_player": simplify_player_for_previous_event(event.get("owner_player")),
        }

    if event_type == "bomb_event":
        return {
            "event_type": "bomb_event",
            "state_after": event.get("state_after"),
        }

    if event_type == "round_result":
        return {
            "event_type": "round_result",
            "winner": event.get("winner"),
            "winner_score": event.get("winner_score"),
        }

    if event_type == "game_over":
        return {
            "event_type": "game_over",
            "winner": event.get("winner"),
            "final_score": event.get("final_score"),
        }

    return {"event_type": event_type} if event_type else {}


def build_previous_events_summary(events):
    events = [as_dict(event) for event in events if as_dict(event).get("event_type") != "team_counter"]
    if not events:
        return []

    primary_event = max(events, key=event_priority)
    simplified = simplify_event_for_previous_context(primary_event)
    return [simplified] if simplified else []


def build_training_wrapper(filtered_batch, current_snapshot, payload_sequence, previous_events):
    current_events = simplify_filtered_batch_for_training(filtered_batch)
    context = build_training_context(current_snapshot)
    map_name = as_dict(as_dict(current_snapshot).get("map")).get("name")
    derived_tactical_summary = build_derived_tactical_summary(
        map_name=map_name,
        alive_players=context.get("alive_players", []),
        current_events=current_events,
        previous_events=previous_events,
        bomb_state=context.get("bomb_state"),
        score=context.get("score"),
    )
    return {
        "input": {
            "context": context,
            "previous_events": copy.deepcopy(previous_events),
            "current_events": current_events,
            "derived_tactical_summary": derived_tactical_summary,
            "request": build_request("event_bundle"),
        }
    }


def build_recent_event_summary(training_wrapper):
    wrapper = as_dict(training_wrapper)
    return build_previous_events_summary(as_dict(wrapper.get("input")).get("current_events", []))


def build_idle_wrapper(current_snapshot, previous_events, mode):
    context = build_training_context(current_snapshot)
    map_name = as_dict(as_dict(current_snapshot).get("map")).get("name")
    derived_tactical_summary = build_derived_tactical_summary(
        map_name=map_name,
        alive_players=context.get("alive_players", []),
        current_events=[],
        previous_events=[],
        bomb_state=context.get("bomb_state"),
        score=context.get("score"),
    )
    return {
        "input": {
            "context": context,
            "previous_events": [],
            "current_events": [],
            "derived_tactical_summary": derived_tactical_summary,
            "request": build_request(mode),
        }
    }


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
            previous_events = copy.deepcopy(PIPELINE_STATE.previous_event_summary or [])
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
        if now_ts - last_interval_prompt_at < interval_seconds():
            continue

        interval_mode = next_interval_mode()
        idle_wrapper = build_idle_wrapper(current_snapshot, previous_events, interval_mode)
        append_pretty_json_record(TRAINING_WRAPPER_PATH, idle_wrapper)
        write_pretty_json_file(TRAINING_WRAPPER_LATEST_PATH, idle_wrapper)
        start_background_prompt_thread(
            process_interval_wrapper,
            idle_wrapper,
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

            if should_reset_for_new_session(previous_snapshot, current_snapshot):
                PIPELINE_STATE.previous_snapshot = None
                previous_snapshot = None
                reset_runtime_session_state(keep_current_snapshot=True)
                slim_log(
                    "session reset",
                    commentary=(
                        f"Detected new map/session -> {snapshot_map_identity(current_snapshot).get('map_name') or 'unknown map'} "
                        f"round {snapshot_map_identity(current_snapshot).get('map_round')}"
                    ),
                    include_commentary=True,
                )

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

        if filtered_batch["events"]:
            with STATE_LOCK:
                previous_events = copy.deepcopy(PIPELINE_STATE.previous_event_summary or [])

            training_wrapper = build_training_wrapper(
                filtered_batch,
                current_snapshot,
                payload_sequence,
                previous_events,
            )

            append_pretty_json_record(FILTERED_EVENTS_PATH, filtered_batch)
            write_pretty_json_file(FILTERED_EVENTS_LATEST_PATH, filtered_batch)
            append_pretty_json_record(TRAINING_WRAPPER_PATH, training_wrapper)
            write_pretty_json_file(TRAINING_WRAPPER_LATEST_PATH, training_wrapper)

            with STATE_LOCK:
                PIPELINE_STATE.previous_event_summary = build_recent_event_summary(training_wrapper)
                PIPELINE_STATE.last_event_prompt_at = time.time()

            slim_log(
                "filtered",
                commentary=f"payload #{payload_sequence} -> {len(filtered_batch['events'])} filtered event(s) + training wrapper",
                include_commentary=True,
            )
            prompt_wrapper = copy.deepcopy(training_wrapper)
        else:
            prompt_wrapper = None

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
                snapshot=current_snapshot,
            )

    def log_message(self, format, *args):
        return


def main():
    reset_session_files()
    reset_prompt_runtime_state()
    PIPELINE_STATE.payload_count = 0
    reset_runtime_session_state()
    append_log(f"[{now_stamp()}] pipeline v3 session started\n")

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    slim_log("startup", commentary=f"Listening on http://{HOST}:{PORT}", include_commentary=True)
    slim_log("startup", commentary=f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", include_commentary=True)
    slim_log("startup", commentary=f"Raw GSI history: {RAW_GSI_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Raw GSI latest: {RAW_GSI_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Filtered history: {FILTERED_EVENTS_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Filtered latest: {FILTERED_EVENTS_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Training history: {TRAINING_WRAPPER_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Training latest: {TRAINING_WRAPPER_LATEST_PATH}", include_commentary=True)
    slim_log("startup", commentary=f"Pipeline log: {PIPELINE_LOG}", include_commentary=True)
    slim_log(
        "startup",
        commentary=(
            "This listener stores pretty-printed raw JSON, filtered JSON, and a training-facing wrapper, "
            "and also runs prompt + TTS experiments for event and idle intervals."
        ),
        include_commentary=True,
    )

    start_background_prompt_thread(interval_prompt_loop)
    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
