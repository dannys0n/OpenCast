import importlib.util
import json
import tempfile
import unittest
from collections import deque
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prompt_queue_v2.py")
SPEC = importlib.util.spec_from_file_location("prompt_queue_v2", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PromptQueueV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

        self.original_state_dir = MODULE.STATE_DIR
        self.original_history_path = MODULE.PROMPT_QUEUE_HISTORY_PATH
        self.original_latest_path = MODULE.PROMPT_QUEUE_LATEST_PATH
        self.original_state_path = MODULE.PROMPT_QUEUE_STATE_PATH
        self.original_next_job_id = MODULE.PROMPT_QUEUE_STATE.next_job_id
        self.original_pending_jobs = deque(MODULE.PROMPT_QUEUE_STATE.pending_jobs)

        MODULE.STATE_DIR = self.state_dir
        MODULE.PROMPT_QUEUE_HISTORY_PATH = self.state_dir / "prompt_queue_pretty.jsonl"
        MODULE.PROMPT_QUEUE_LATEST_PATH = self.state_dir / "prompt_queue_latest.json"
        MODULE.PROMPT_QUEUE_STATE_PATH = self.state_dir / "prompt_queue_state.json"
        MODULE.PROMPT_QUEUE_STATE.next_job_id = 1
        MODULE.PROMPT_QUEUE_STATE.pending_jobs = deque()

    def tearDown(self):
        MODULE.STATE_DIR = self.original_state_dir
        MODULE.PROMPT_QUEUE_HISTORY_PATH = self.original_history_path
        MODULE.PROMPT_QUEUE_LATEST_PATH = self.original_latest_path
        MODULE.PROMPT_QUEUE_STATE_PATH = self.original_state_path
        MODULE.PROMPT_QUEUE_STATE.next_job_id = self.original_next_job_id
        MODULE.PROMPT_QUEUE_STATE.pending_jobs = self.original_pending_jobs
        self.temp_dir.cleanup()

    def test_enqueue_prompt_job_writes_history_latest_and_queue_state(self):
        MODULE.reset_prompt_queue_state()

        filtered_batch = {
            "payload_sequence": 12,
            "important_delta_paths": ["allplayers.*.match_stats.kills"],
            "events": [
                {
                    "event_index": 1,
                    "event_type": "kill",
                    "players": {
                        "killer": {"name": "Uri", "team": "T"},
                        "victim": {"name": "Maru", "team": "CT"},
                    },
                }
            ],
        }

        prompt_job = MODULE.enqueue_prompt_job(filtered_batch)

        self.assertEqual(prompt_job["job_id"], 1)
        self.assertEqual(prompt_job["status"], "pending")
        self.assertEqual(prompt_job["payload_sequence"], 12)
        self.assertEqual(prompt_job["event_types"], ["kill"])
        self.assertEqual(prompt_job["prompt_input"]["events"], filtered_batch["events"])

        latest_record = json.loads(MODULE.PROMPT_QUEUE_LATEST_PATH.read_text())
        self.assertEqual(latest_record["job_id"], 1)
        self.assertEqual(latest_record["payload_sequence"], 12)

        queue_state = json.loads(MODULE.PROMPT_QUEUE_STATE_PATH.read_text())
        self.assertEqual(queue_state["pending_count"], 1)
        self.assertEqual(queue_state["next_job_id"], 2)
        self.assertEqual(queue_state["pending_jobs"][0]["job_id"], 1)
        self.assertEqual(queue_state["pending_jobs"][0]["event_types"], ["kill"])

        history_text = MODULE.PROMPT_QUEUE_HISTORY_PATH.read_text()
        self.assertIn('"job_id": 1', history_text)
        self.assertIn('"payload_sequence": 12', history_text)


if __name__ == "__main__":
    unittest.main()
