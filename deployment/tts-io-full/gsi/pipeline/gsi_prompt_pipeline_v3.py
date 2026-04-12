import copy
import json
import os
import signal
import subprocess
import threading
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
    filter_important_events,
)


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state" / "v3"
RAW_GSI_PATH = STATE_DIR / "gsi_received_pretty.jsonl"
RAW_GSI_LATEST_PATH = STATE_DIR / "gsi_received_latest.json"
FILTERED_EVENTS_PATH = STATE_DIR / "gsi_filtered_pretty.jsonl"
FILTERED_EVENTS_LATEST_PATH = STATE_DIR / "gsi_filtered_latest.json"
PIPELINE_LOG = STATE_DIR / "pipeline_v3.log"

STATE_LOCK = threading.Lock()


@dataclass
class PipelineState:
    latest_snapshot: dict | None = None
    previous_snapshot: dict | None = None
    payload_count: int = 0


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
        PIPELINE_LOG,
    ]:
        path.write_text("", encoding="utf-8")


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

        if filtered_batch["events"]:
            append_pretty_json_record(FILTERED_EVENTS_PATH, filtered_batch)
            write_pretty_json_file(FILTERED_EVENTS_LATEST_PATH, filtered_batch)
            print(
                f"[gsi-v3] #{payload_sequence} stored raw payload and emitted "
                f"{len(filtered_batch['events'])} filtered event(s)",
                flush=True,
            )
        else:
            print(f"[gsi-v3] #{payload_sequence} stored raw payload with no important events", flush=True)

        try:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        except OSError:
            return

    def log_message(self, format, *args):
        return


def main():
    reset_session_files()
    PIPELINE_STATE.latest_snapshot = None
    PIPELINE_STATE.previous_snapshot = None
    PIPELINE_STATE.payload_count = 0
    append_log(f"[{now_stamp()}] pipeline v3 session started\n")

    if KILL_EXISTING_LISTENER:
        reclaim_port(PORT)

    print(f"Listening on http://{HOST}:{PORT}", flush=True)
    print(f"Auth required: {'yes' if EXPECTED_TOKEN else 'no'}", flush=True)
    print(f"Raw GSI history:    {RAW_GSI_PATH}", flush=True)
    print(f"Raw GSI latest:     {RAW_GSI_LATEST_PATH}", flush=True)
    print(f"Filtered history:   {FILTERED_EVENTS_PATH}", flush=True)
    print(f"Filtered latest:    {FILTERED_EVENTS_LATEST_PATH}", flush=True)
    print(f"Pipeline log:       {PIPELINE_LOG}", flush=True)
    print(
        "This v3 listener stores pretty-printed raw and filtered JSON only, "
        "with no prompt or TTS handoff. It is meant for dataset capture and "
        "synthetic training-data workflow support.",
        flush=True,
    )

    ReusableThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
