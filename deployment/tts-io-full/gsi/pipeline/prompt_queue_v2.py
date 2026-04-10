import json
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
STATE_DIR = SCRIPT_DIR / ".state" / "v2"
PROMPT_QUEUE_HISTORY_PATH = STATE_DIR / "prompt_queue_pretty.jsonl"
PROMPT_QUEUE_LATEST_PATH = STATE_DIR / "prompt_queue_latest.json"
PROMPT_QUEUE_STATE_PATH = STATE_DIR / "prompt_queue_state.json"

QUEUE_LOCK = threading.Lock()


@dataclass
class PromptQueueState:
    next_job_id: int = 1
    pending_jobs: deque | None = None

    def __post_init__(self):
        if self.pending_jobs is None:
            self.pending_jobs = deque()


PROMPT_QUEUE_STATE = PromptQueueState()


def now_stamp():
    return datetime.now().isoformat(timespec="seconds")


def ensure_state_dir():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    for path in [
        PROMPT_QUEUE_HISTORY_PATH,
        PROMPT_QUEUE_LATEST_PATH,
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


def reset_prompt_queue_state():
    ensure_state_dir()
    with QUEUE_LOCK:
        PROMPT_QUEUE_STATE.next_job_id = 1
        PROMPT_QUEUE_STATE.pending_jobs = deque()

        for path in [
            PROMPT_QUEUE_HISTORY_PATH,
            PROMPT_QUEUE_LATEST_PATH,
            PROMPT_QUEUE_STATE_PATH,
        ]:
            path.write_text("", encoding="utf-8")


def build_prompt_job(filtered_batch):
    event_types = [event.get("event_type") for event in filtered_batch.get("events", [])]
    return {
        "job_id": PROMPT_QUEUE_STATE.next_job_id,
        "created_at": now_stamp(),
        "status": "pending",
        "source": "gsi_filtered_events",
        "payload_sequence": filtered_batch.get("payload_sequence"),
        "event_count": len(filtered_batch.get("events", [])),
        "event_types": event_types,
        "prompt_input": {
            "important_delta_paths": filtered_batch.get("important_delta_paths", []),
            "events": filtered_batch.get("events", []),
        },
        "stub": {
            "llm_request_prepared": True,
            "llm_request_sent": False,
            "llm_response_received": False,
            "notes": "Stub queue item only. Real prompt assembly/dispatch is not implemented yet.",
        },
    }


def build_queue_state_record():
    pending_jobs = list(PROMPT_QUEUE_STATE.pending_jobs)
    return {
        "updated_at": now_stamp(),
        "next_job_id": PROMPT_QUEUE_STATE.next_job_id,
        "pending_count": len(pending_jobs),
        "pending_jobs": pending_jobs,
    }


def enqueue_prompt_job(filtered_batch):
    if not filtered_batch.get("events"):
        return None

    with QUEUE_LOCK:
        prompt_job = build_prompt_job(filtered_batch)
        PROMPT_QUEUE_STATE.pending_jobs.append(
            {
                "job_id": prompt_job["job_id"],
                "payload_sequence": prompt_job["payload_sequence"],
                "event_count": prompt_job["event_count"],
                "event_types": prompt_job["event_types"],
                "status": prompt_job["status"],
                "created_at": prompt_job["created_at"],
            }
        )
        PROMPT_QUEUE_STATE.next_job_id += 1

        append_pretty_json_record(PROMPT_QUEUE_HISTORY_PATH, prompt_job)
        write_pretty_json_file(PROMPT_QUEUE_LATEST_PATH, prompt_job)
        write_pretty_json_file(PROMPT_QUEUE_STATE_PATH, build_queue_state_record())

    return prompt_job

