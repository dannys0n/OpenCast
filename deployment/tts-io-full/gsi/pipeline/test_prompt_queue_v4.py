import importlib.util
import json
import sys
import tempfile
import unittest
from collections import deque
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prompt_queue_v4.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("prompt_queue_v4", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PromptQueueV4Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

        self.original_state_dir = MODULE.STATE_DIR
        self.original_runtime_history_path = MODULE.PROMPT_RUNTIME_HISTORY_PATH
        self.original_runtime_latest_path = MODULE.PROMPT_RUNTIME_LATEST_PATH
        self.original_queue_state_path = MODULE.PROMPT_QUEUE_STATE_PATH
        self.original_chemistry_lines_path = MODULE.CHEMISTRY_LINES_PATH
        self.original_ensure_queue_monitor = MODULE.ensure_queue_monitor
        self.original_ensure_queue_worker = MODULE.ensure_queue_worker
        self.original_playback_queue = MODULE.PLAYBACK_QUEUE
        self.original_current_bundle = MODULE.CURRENT_BUNDLE
        self.original_current_item = MODULE.CURRENT_ITEM
        self.original_queue_monitor_thread = MODULE.QUEUE_MONITOR_THREAD
        self.original_item_sequence = MODULE.ITEM_SEQUENCE
        self.original_bundle_sequence = MODULE.BUNDLE_SEQUENCE
        self.original_event_queue_overflow_started_at = MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT
        self.original_request_chat_completion = MODULE.request_chat_completion
        self.original_build_text_llm_config = MODULE.build_text_llm_config
        self.original_idle_mode_index = MODULE.IDLE_MODE_INDEX

        MODULE.STATE_DIR = self.state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.state_dir / "prompt_runtime_pretty.jsonl"
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.state_dir / "prompt_runtime_latest.json"
        MODULE.PROMPT_QUEUE_STATE_PATH = self.state_dir / "prompt_queue_state.json"
        MODULE.CHEMISTRY_LINES_PATH = self.state_dir / "chemistry_lines_v4.json"
        MODULE.ensure_queue_monitor = lambda: None
        MODULE.ensure_queue_worker = lambda repo_root: None
        MODULE.PLAYBACK_QUEUE = deque()
        MODULE.CURRENT_BUNDLE = None
        MODULE.CURRENT_ITEM = None
        MODULE.QUEUE_MONITOR_THREAD = None
        MODULE.ITEM_SEQUENCE = 0
        MODULE.BUNDLE_SEQUENCE = 0
        MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT = None
        MODULE.IDLE_MODE_INDEX = 0

    def tearDown(self):
        MODULE.STATE_DIR = self.original_state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.original_runtime_history_path
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.original_runtime_latest_path
        MODULE.PROMPT_QUEUE_STATE_PATH = self.original_queue_state_path
        MODULE.CHEMISTRY_LINES_PATH = self.original_chemistry_lines_path
        MODULE.ensure_queue_monitor = self.original_ensure_queue_monitor
        MODULE.ensure_queue_worker = self.original_ensure_queue_worker
        MODULE.PLAYBACK_QUEUE = self.original_playback_queue
        MODULE.CURRENT_BUNDLE = self.original_current_bundle
        MODULE.CURRENT_ITEM = self.original_current_item
        MODULE.QUEUE_MONITOR_THREAD = self.original_queue_monitor_thread
        MODULE.ITEM_SEQUENCE = self.original_item_sequence
        MODULE.BUNDLE_SEQUENCE = self.original_bundle_sequence
        MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT = self.original_event_queue_overflow_started_at
        MODULE.request_chat_completion = self.original_request_chat_completion
        MODULE.build_text_llm_config = self.original_build_text_llm_config
        MODULE.IDLE_MODE_INDEX = self.original_idle_mode_index
        self.temp_dir.cleanup()

    def test_parse_json_line_array_reads_explicit_json_array(self):
        parsed = MODULE.parse_json_line_array(
            '["Niko doubles", "The A site is cracked."]',
            2,
        )

        self.assertEqual(parsed, ["Niko doubles", "The A site is cracked."])

    def test_event_queue_overflow_timer_drops_one_oldest_event_bundle_after_five_seconds(self):
        first_bundle = MODULE.build_bundle(
            kind="event",
            items=[MODULE.build_queue_item(commentary="First event", caster="caster0", prompt_style="event_trigger", tag="event", payload_sequence=1, source="event")],
            payload_sequence=1,
            source="event",
        )
        second_bundle = MODULE.build_bundle(
            kind="event",
            items=[MODULE.build_queue_item(commentary="Second event", caster="caster0", prompt_style="event_trigger", tag="event", payload_sequence=2, source="event")],
            payload_sequence=2,
            source="event",
        )

        MODULE.PLAYBACK_QUEUE.append(first_bundle)
        MODULE.PLAYBACK_QUEUE.append(second_bundle)

        MODULE.refresh_event_queue_overflow_timer_locked(now_monotonic=100.0)
        self.assertEqual(MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT, 100.0)
        self.assertAlmostEqual(MODULE.seconds_until_event_queue_drop_locked(now_monotonic=103.0), 2.0)

        dropped = MODULE.dequeue_one_overflow_event_bundle_if_due_locked(now_monotonic=105.0)

        self.assertEqual(dropped["id"], first_bundle["id"])
        self.assertEqual([bundle["id"] for bundle in MODULE.PLAYBACK_QUEUE], [second_bundle["id"]])
        self.assertIsNone(MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT)

    def test_event_queue_overflow_timer_restarts_if_two_or_more_events_remain_after_drop(self):
        bundles = []
        for payload_sequence in (1, 2, 3):
            bundles.append(
                MODULE.build_bundle(
                    kind="event",
                    items=[
                        MODULE.build_queue_item(
                            commentary=f"Event {payload_sequence}",
                            caster="caster0",
                            prompt_style="event_trigger",
                            tag="event",
                            payload_sequence=payload_sequence,
                            source="event",
                        )
                    ],
                    payload_sequence=payload_sequence,
                    source="event",
                )
            )

        for bundle in bundles:
            MODULE.PLAYBACK_QUEUE.append(bundle)

        MODULE.refresh_event_queue_overflow_timer_locked(now_monotonic=100.0)
        dropped = MODULE.dequeue_one_overflow_event_bundle_if_due_locked(now_monotonic=105.0)

        self.assertEqual(dropped["id"], bundles[0]["id"])
        self.assertEqual([bundle["id"] for bundle in MODULE.PLAYBACK_QUEUE], [bundles[1]["id"], bundles[2]["id"]])
        self.assertEqual(MODULE.EVENT_QUEUE_OVERFLOW_STARTED_AT, 105.0)

    def test_process_event_wrapper_queues_single_event_bundle_with_event_and_analysis(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": '["Niko doubles on A long", "That should break the rotate timing."]',
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        wrapper = {
            "input": {
                "event_descriptions": ["Niko kills Broky", "2 kills for Niko"],
                "current_events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Niko", "team": "T", "round_kills": 2},
                        "victim": {"name": "Broky", "team": "CT"},
                    }
                ],
                "request": {
                    "mode": "event_trigger",
                    "output_count": 2,
                },
                "analysis_caster": "caster1",
            }
        }

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=12)

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["queued_bundle"]["kind"], "event")
        self.assertEqual(len(record["queued_bundle"]["items"]), 2)
        self.assertEqual(record["queued_bundle"]["items"][0]["caster"], "caster0")
        self.assertEqual(record["queued_bundle"]["items"][0]["commentary"], "Niko doubles on A long")
        self.assertEqual(record["queued_bundle"]["items"][1]["caster"], "caster1")
        self.assertIn('"event_descriptions"', captured["user_prompt"])
        self.assertIn('"current_events"', captured["user_prompt"])

    def test_process_interval_wrapper_idle_analysis_queues_three_items(self):
        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": '["Mid is stretched thin.", "That spacing is asking for a punish.", "One timing hit could tear it open."]',
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        wrapper = {
            "input": {
                "score": {"CT": 5, "T": 7},
                "player_locations": [{"name": "Niko", "team": "T", "map_callout": "A Ramp"}],
                "request": {"mode": "idle_analysis", "output_count": 3},
            }
        }

        record = MODULE.process_interval_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=14, interval_mode="idle_analysis")

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["mode"], "idle_analysis")
        self.assertEqual([item["caster"] for item in record["queued_bundle"]["items"]], ["caster0", "caster1", "caster0"])

    def test_process_interval_wrapper_chemistry_skips_llm_and_uses_json_set(self):
        chemistry_sets = [
            [
                {"caster": "caster1", "text": "First line."},
                {"caster": "caster0", "text": "Second line."},
                {"caster": "caster1", "text": "Third line."}
            ]
        ]
        MODULE.CHEMISTRY_LINES_PATH.write_text(json.dumps(chemistry_sets), encoding="utf-8")

        def fail_request(*args, **kwargs):
            raise AssertionError("LLM should not be called for chemistry mode")

        MODULE.request_chat_completion = fail_request

        wrapper = {
            "input": {
                "score": {"CT": 5, "T": 7},
                "player_locations": [{"name": "Niko", "team": "T", "map_callout": "A Ramp"}],
                "request": {"mode": "chemistry", "output_count": 3},
            }
        }

        record = MODULE.process_interval_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=15, interval_mode="chemistry")

        self.assertEqual(record["status"], "completed")
        self.assertEqual([item["caster"] for item in record["queued_bundle"]["items"]], ["caster1", "caster0", "caster1"])
        self.assertEqual([item["commentary"] for item in record["queued_bundle"]["items"]], ["First line.", "Second line.", "Third line."])


if __name__ == "__main__":
    unittest.main()
