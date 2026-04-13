import importlib.util
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prompt_queue_v3.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("prompt_queue_v3", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PromptQueueV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

        self.original_state_dir = MODULE.STATE_DIR
        self.original_runtime_history_path = MODULE.PROMPT_RUNTIME_HISTORY_PATH
        self.original_runtime_latest_path = MODULE.PROMPT_RUNTIME_LATEST_PATH
        self.original_queue_state_path = MODULE.PROMPT_QUEUE_STATE_PATH
        self.original_ensure_queue_worker = MODULE.ensure_queue_worker
        self.original_playback_queue = MODULE.PLAYBACK_QUEUE
        self.original_current_playback = MODULE.CURRENT_PLAYBACK
        self.original_item_sequence = MODULE.ITEM_SEQUENCE

        MODULE.STATE_DIR = self.state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.state_dir / "prompt_runtime_pretty.jsonl"
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.state_dir / "prompt_runtime_latest.json"
        MODULE.PROMPT_QUEUE_STATE_PATH = self.state_dir / "prompt_queue_state.json"
        MODULE.ensure_queue_worker = lambda repo_root: None
        MODULE.PLAYBACK_QUEUE = deque()
        MODULE.CURRENT_PLAYBACK = None
        MODULE.ITEM_SEQUENCE = 0

    def tearDown(self):
        MODULE.STATE_DIR = self.original_state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.original_runtime_history_path
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.original_runtime_latest_path
        MODULE.PROMPT_QUEUE_STATE_PATH = self.original_queue_state_path
        MODULE.ensure_queue_worker = self.original_ensure_queue_worker
        MODULE.PLAYBACK_QUEUE = self.original_playback_queue
        MODULE.CURRENT_PLAYBACK = self.original_current_playback
        MODULE.ITEM_SEQUENCE = self.original_item_sequence
        self.temp_dir.cleanup()

    def test_extract_commentary_lines_prefers_clean_lines(self):
        raw_text = "1. Colin drops Maru.\n2. CT close the round."
        self.assertEqual(
            MODULE.extract_commentary_lines(raw_text, expected_max=2),
            ["Colin drops Maru.", "CT close the round."],
        )

    def test_should_ignore_pure_grenade_throw_during_spectate(self):
        wrapper = {
            "input": {
                "current_events": [
                    {
                        "event_type": "grenade_thrown",
                        "grenade_type": "smoke",
                    }
                ]
            }
        }
        snapshot = {"allplayers": {"2": {"name": "Uri"}}}
        self.assertTrue(MODULE.should_ignore_event_prompt(wrapper, snapshot))

    def test_enqueue_event_interrupts_current_non_event_and_drops_queued_non_events(self):
        current_non_event = {
            "id": 10,
            "tag": "color",
            "caster": "color",
            "prompt_style": "idle_color",
            "commentary": "Quiet for now.",
            "payload_sequence": 5,
            "source": "idle_color",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        queued_non_event = {
            "id": 11,
            "tag": "followup",
            "caster": "color",
            "prompt_style": "play_by_play_follow_up",
            "commentary": "Plant forces the retake.",
            "payload_sequence": 5,
            "source": "event",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.CURRENT_PLAYBACK = current_non_event
        MODULE.PLAYBACK_QUEUE.append(queued_non_event)

        event_item = MODULE.build_queue_item(
            commentary="Double kill for Yanni.",
            caster="play_by_play",
            prompt_style="play_by_play_event",
            tag="event",
            payload_sequence=6,
            source="event",
        )
        followup_item = MODULE.build_queue_item(
            commentary="CT take control.",
            caster="color",
            prompt_style="play_by_play_follow_up",
            tag="followup",
            payload_sequence=6,
            source="event",
        )

        dropped = MODULE.enqueue_prompt_items([event_item, followup_item], Path("/tmp/opencast"))

        self.assertTrue(current_non_event["interrupt_event"].is_set())
        self.assertEqual([item["id"] for item in dropped], [11])
        self.assertEqual(
            [(item["tag"], item["commentary"]) for item in MODULE.PLAYBACK_QUEUE],
            [
                ("event", "Double kill for Yanni."),
                ("followup", "CT take control."),
            ],
        )


if __name__ == "__main__":
    unittest.main()
