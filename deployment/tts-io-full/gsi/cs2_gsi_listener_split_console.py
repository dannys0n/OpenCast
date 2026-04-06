import copy
import hashlib
import json
import os
import signal
import threading
import time
import subprocess
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
LOG_DIR = SCRIPT_DIR / ".console"
INTERVAL_LOG = LOG_DIR / "interval.log"
EVENTS_LOG = LOG_DIR / "events.log"
REPORT_JSON = LOG_DIR / "runtime_report.json"
REPORT_LOG = LOG_DIR / "runtime_report.log"


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
SNAPSHOT_INTERVAL_SECONDS = env_float("CS2_GSI_SNAPSHOT_INTERVAL", 2.0)
KILL_EXISTING_LISTENER = env_bool("CS2_GSI_KILL_EXISTING_LISTENER", True)

STATE_LOCK = threading.Lock()
LATEST_PAYLOAD = None
LATEST_HASH = None
LAST_INTERVAL_HASH = None
PAYLOAD_COUNT = 0
RUNTIME_OBSERVATIONS = {
    "started_at": None,
    "last_updated_at": None,
    "payload_count": 0,
    "top_level_keys_seen": [],
    "top_level_non_empty": [],
    "normalized_paths_seen": [],
    "normalized_non_empty_paths": [],
    "signal_flags": {
        "player_position_seen": False,
        "allplayers_position_seen": False,
        "grenades_block_seen": False,
        "grenades_block_non_empty": False,
        "allgrenades_block_seen": False,
        "allgrenades_block_non_empty": False,
        "grenade_position_seen": False,
    },
    "latest_non_empty_samples": {
        "grenades": None,
        "allgrenades": None,
        "player_position": None,
        "allplayers_positions": None,
        "grenade_positions": None,
    },
}


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def stable_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def payload_hash(payload):
    return hashlib.sha1(stable_json(payload).encode("utf-8")).hexdigest()


def as_dict(value):
    return value if isinstance(value, dict) else {}


def normalize_path(path):
    parts = str(path).split(".")
    normalized = ["*" if part.isdigit() else part for part in parts]
    return ".".join(normalized)


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


def flatten_paths(node, prefix=""):
    paths = set()
    if isinstance(node, dict):
        for key, value in node.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            paths.add(next_prefix)
            paths.update(flatten_paths(value, next_prefix))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            next_prefix = f"{prefix}.{index}" if prefix else str(index)
            paths.add(next_prefix)
            paths.update(flatten_paths(value, next_prefix))
    return paths


def non_empty_paths(node, prefix=""):
    paths = set()
    if isinstance(node, dict):
        if node and prefix:
            paths.add(prefix)
        for key, value in node.items():
            next_prefix = f"{prefix}.{key}" if prefix else str(key)
            if value not in (None, "", [], {}):
                paths.add(next_prefix)
            paths.update(non_empty_paths(value, next_prefix))
    elif isinstance(node, list):
        if node and prefix:
            paths.add(prefix)
        for index, value in enumerate(node):
            next_prefix = f"{prefix}.{index}" if prefix else str(index)
            if value not in (None, "", [], {}):
                paths.add(next_prefix)
            paths.update(non_empty_paths(value, next_prefix))
    return paths


def compact_copy(value):
    if isinstance(value, dict):
        return {key: compact_copy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [compact_copy(item) for item in value]
    return value


def collect_allplayer_positions(allplayers):
    positions = {}
    for slot, player in as_dict(allplayers).items():
        position = as_dict(player).get("position")
        if position not in (None, ""):
            positions[str(slot)] = position
    return positions


def collect_grenade_positions(block):
    positions = {}
    for key, grenade in as_dict(block).items():
        grenade_dict = as_dict(grenade)
        position = grenade_dict.get("position")
        if position not in (None, ""):
            positions[str(key)] = {
                "position": position,
                "owner": grenade_dict.get("owner"),
                "type": grenade_dict.get("type") or grenade_dict.get("weapon"),
            }
    return positions


def summarize_payload(payload):
    player = as_dict(payload.get("player"))
    player_state = as_dict(player.get("state"))
    map_data = as_dict(payload.get("map"))
    round_data = as_dict(payload.get("round"))
    bomb_data = as_dict(payload.get("bomb"))
    provider = as_dict(payload.get("provider"))
    allplayers = as_dict(payload.get("allplayers"))
    grenades = as_dict(payload.get("grenades"))
    allgrenades = as_dict(payload.get("allgrenades"))

    summary = {
        "provider": provider.get("name"),
        "map": map_data.get("name"),
        "map_phase": map_data.get("phase"),
        "round_phase": round_data.get("phase"),
        "round_win_team": round_data.get("win_team"),
        "bomb_state": bomb_data.get("state"),
        "player": player.get("name"),
        "team": player.get("team"),
        "activity": player.get("activity"),
        "health": player_state.get("health"),
        "armor": player_state.get("armor"),
        "allplayers_count": len(allplayers),
        "grenades_count": len(grenades),
        "allgrenades_count": len(allgrenades),
        "player_position": player.get("position"),
        "top_level_keys": sorted(payload.keys()),
    }

    return {key: value for key, value in summary.items() if value not in (None, "", [], {})}


def build_interval_entry(payload):
    stamp = now_stamp()
    summary = summarize_payload(payload)
    lines = [
        "",
        "=" * 88,
        f"INTERVAL SNAPSHOT @ {stamp}",
        "=" * 88,
        json.dumps(summary, indent=2, sort_keys=True),
        "-" * 88,
        json.dumps(payload, indent=2, sort_keys=True),
    ]
    return "\n".join(lines) + "\n"


def build_event_entry(payload):
    stamp = now_stamp()
    changed = sorted(
        set(extract_changed_paths(payload.get("added")) + extract_changed_paths(payload.get("previously")))
    )
    event_view = {
        "summary": summarize_payload(payload),
        "changed_paths": changed,
        "added": payload.get("added", {}),
        "previously": payload.get("previously", {}),
    }

    lines = [
        "",
        "=" * 88,
        f"EVENT PAYLOAD @ {stamp}",
        "=" * 88,
        json.dumps(event_view, indent=2, sort_keys=True),
    ]
    return "\n".join(lines) + "\n"


def append_log(path, text):
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()


def write_runtime_report():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_JSON.write_text(
        json.dumps(RUNTIME_OBSERVATIONS, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def append_runtime_note(text):
    append_log(REPORT_LOG, text)


def note_signal(flag_name, message):
    if not RUNTIME_OBSERVATIONS["signal_flags"].get(flag_name):
        RUNTIME_OBSERVATIONS["signal_flags"][flag_name] = True
        append_runtime_note(f"[{now_stamp()}] {message}\n")


def update_runtime_observations(payload):
    stamp = now_stamp()
    top_level_keys = set(payload.keys())
    top_level_non_empty = {
        key for key in payload.keys() if payload.get(key) not in (None, "", [], {})
    }

    seen_paths = {normalize_path(path) for path in flatten_paths(payload)}
    non_empty_seen_paths = {normalize_path(path) for path in non_empty_paths(payload)}

    RUNTIME_OBSERVATIONS["last_updated_at"] = stamp
    RUNTIME_OBSERVATIONS["payload_count"] = PAYLOAD_COUNT
    RUNTIME_OBSERVATIONS["top_level_keys_seen"] = sorted(
        set(RUNTIME_OBSERVATIONS["top_level_keys_seen"]) | top_level_keys
    )
    RUNTIME_OBSERVATIONS["top_level_non_empty"] = sorted(
        set(RUNTIME_OBSERVATIONS["top_level_non_empty"]) | top_level_non_empty
    )
    RUNTIME_OBSERVATIONS["normalized_paths_seen"] = sorted(
        set(RUNTIME_OBSERVATIONS["normalized_paths_seen"]) | seen_paths
    )
    RUNTIME_OBSERVATIONS["normalized_non_empty_paths"] = sorted(
        set(RUNTIME_OBSERVATIONS["normalized_non_empty_paths"]) | non_empty_seen_paths
    )

    player = as_dict(payload.get("player"))
    player_position = player.get("position")
    if player_position not in (None, ""):
        note_signal("player_position_seen", f"Observed player.position = {player_position}")
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"]["player_position"] = player_position

    allplayers_positions = collect_allplayer_positions(payload.get("allplayers"))
    if allplayers_positions:
        note_signal(
            "allplayers_position_seen",
            f"Observed allplayers positions for {len(allplayers_positions)} player(s)",
        )
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"]["allplayers_positions"] = compact_copy(
            allplayers_positions
        )

    if "grenades" in payload:
        note_signal("grenades_block_seen", "Observed top-level grenades block")
    grenades = as_dict(payload.get("grenades"))
    if grenades:
        note_signal("grenades_block_non_empty", "Observed non-empty top-level grenades block")
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"]["grenades"] = compact_copy(grenades)

    if "allgrenades" in payload:
        note_signal("allgrenades_block_seen", "Observed top-level allgrenades block")
    allgrenades = as_dict(payload.get("allgrenades"))
    if allgrenades:
        note_signal("allgrenades_block_non_empty", "Observed non-empty allgrenades block")
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"]["allgrenades"] = compact_copy(allgrenades)

    grenade_positions = {}
    grenade_positions.update(collect_grenade_positions(grenades))
    grenade_positions.update(collect_grenade_positions(allgrenades))
    if grenade_positions:
        note_signal(
            "grenade_position_seen",
            f"Observed grenade position data for {len(grenade_positions)} grenade object(s)",
        )
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"]["grenade_positions"] = compact_copy(
            grenade_positions
        )

    write_runtime_report()


def reset_logs():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    INTERVAL_LOG.write_text("", encoding="utf-8")
    EVENTS_LOG.write_text("", encoding="utf-8")
    REPORT_LOG.write_text("", encoding="utf-8")
    RUNTIME_OBSERVATIONS["started_at"] = now_stamp()
    RUNTIME_OBSERVATIONS["last_updated_at"] = None
    RUNTIME_OBSERVATIONS["payload_count"] = 0
    RUNTIME_OBSERVATIONS["top_level_keys_seen"] = []
    RUNTIME_OBSERVATIONS["top_level_non_empty"] = []
    RUNTIME_OBSERVATIONS["normalized_paths_seen"] = []
    RUNTIME_OBSERVATIONS["normalized_non_empty_paths"] = []
    for key in list(RUNTIME_OBSERVATIONS["signal_flags"].keys()):
        RUNTIME_OBSERVATIONS["signal_flags"][key] = False
    for key in list(RUNTIME_OBSERVATIONS["latest_non_empty_samples"].keys()):
        RUNTIME_OBSERVATIONS["latest_non_empty_samples"][key] = None
    write_runtime_report()


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
        except PermissionError:
            print(f"Could not terminate PID {pid}: permission denied", flush=True)

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
            print(f"Force-killed PID {pid} on port {port}", flush=True)
        except ProcessLookupError:
            pass
        except PermissionError:
            print(f"Could not force-kill PID {pid}: permission denied", flush=True)


def snapshot_worker():
    global LAST_INTERVAL_HASH

    while True:
        time.sleep(SNAPSHOT_INTERVAL_SECONDS)

        with STATE_LOCK:
            payload = copy.deepcopy(LATEST_PAYLOAD)
            current_hash = LATEST_HASH

        if not payload or not current_hash or current_hash == LAST_INTERVAL_HASH:
            continue

        append_log(INTERVAL_LOG, build_interval_entry(payload))
        LAST_INTERVAL_HASH = current_hash


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        global LATEST_PAYLOAD
        global LATEST_HASH
        global PAYLOAD_COUNT

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

        changed = bool(as_dict(payload.get("added")) or as_dict(payload.get("previously")))
        current_hash = payload_hash(payload)

        with STATE_LOCK:
            LATEST_PAYLOAD = copy.deepcopy(payload)
            LATEST_HASH = current_hash
            PAYLOAD_COUNT += 1

        update_runtime_observations(payload)

        if changed:
            append_log(EVENTS_LOG, build_event_entry(payload))
            print(
                f"[event] #{PAYLOAD_COUNT} @ {now_stamp()} changed payload received; "
                f"see {EVENTS_LOG}",
                flush=True,
            )

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        return


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def main():
    reset_logs()

    thread = threading.Thread(target=snapshot_worker, daemon=True)
    thread.start()

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Snapshot interval: {SNAPSHOT_INTERVAL_SECONDS:.1f}s", flush=True)
    print(f"Interval log: {INTERVAL_LOG}", flush=True)
    print(f"Events log:   {EVENTS_LOG}", flush=True)
    print(f"Report json:  {REPORT_JSON}", flush=True)
    print(f"Report log:   {REPORT_LOG}", flush=True)
    print(
        "Use separate terminals to tail each log so snapshots and event deltas stay readable.",
        flush=True,
    )

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
