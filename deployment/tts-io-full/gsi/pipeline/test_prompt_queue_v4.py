import importlib.util
import json
import sys
import tempfile
import threading
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
        self.original_ensure_queue_worker = MODULE.ensure_queue_worker
        self.original_playback_queue = MODULE.PLAYBACK_QUEUE
        self.original_current_playback = MODULE.CURRENT_PLAYBACK
        self.original_item_sequence = MODULE.ITEM_SEQUENCE
        self.original_request_chat_completion = MODULE.request_chat_completion
        self.original_build_text_llm_config = MODULE.build_text_llm_config
        self.original_interval_mode_index = MODULE.INTERVAL_MODE_INDEX
        self.original_choose_chemistry_line_set = MODULE.choose_chemistry_line_set

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
        MODULE.request_chat_completion = self.original_request_chat_completion
        MODULE.build_text_llm_config = self.original_build_text_llm_config
        MODULE.INTERVAL_MODE_INDEX = self.original_interval_mode_index
        MODULE.choose_chemistry_line_set = self.original_choose_chemistry_line_set
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

    def test_prepare_queue_for_event_trigger_interrupts_current_non_event_and_drops_queued_non_events(self):
        current_non_event = {
            "id": 10,
            "tag": "idle",
            "caster": "caster1",
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
            "caster": "caster1",
            "prompt_style": "play_by_play_follow_up",
            "commentary": "Plant forces the retake.",
            "payload_sequence": 5,
            "source": "event",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.CURRENT_PLAYBACK = current_non_event
        MODULE.PLAYBACK_QUEUE.append(queued_non_event)

        dropped, interrupted_current = MODULE.prepare_queue_for_event_trigger()

        self.assertTrue(current_non_event["interrupt_event"].is_set())
        self.assertEqual(interrupted_current["id"], 10)
        self.assertEqual([item["id"] for item in dropped], [11])
        self.assertEqual(list(MODULE.PLAYBACK_QUEUE), [])

    def test_process_event_wrapper_queues_first_line_as_event_and_rest_as_followups(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": "Niko doubles up.\nThat opens A.\nRotation is late.",
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        current_non_event = {
            "id": 10,
            "tag": "idle",
            "caster": "caster1",
            "prompt_style": "idle_color",
            "commentary": "Quiet for now.",
            "payload_sequence": 11,
            "source": "idle_color",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        queued_non_event = {
            "id": 11,
            "tag": "idle",
            "caster": "caster1",
            "prompt_style": "idle_color",
            "commentary": "Still waiting.",
            "payload_sequence": 11,
            "source": "idle_color",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.CURRENT_PLAYBACK = current_non_event
        MODULE.PLAYBACK_QUEUE.append(queued_non_event)

        wrapper = {
            "input": {
                "context": {
                    "bomb_state": "carried",
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Niko", "team": "T", "map_callout": "A Ramp"}],
                },
                "previous_events": [{"event_type": "bomb_event", "state_after": "planted"}],
                "current_events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Niko", "team": "T", "map_callout": "A Ramp", "round_kills": 2},
                        "victim": {"name": "Broky", "team": "CT"},
                    }
                ],
                "derived_tactical_summary": {
                    "next_move_hint": "a_leaning",
                    "pressure": {"site": "a_leaning"},
                    "confidence": "medium",
                },
            }
        }

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=12, snapshot={})

        self.assertEqual(record["status"], "completed")
        self.assertEqual([item["tag"] for item in record["queued_items"]], ["event", "followup", "followup"])
        self.assertEqual(record["queued_items"][0]["commentary"], "Niko doubles up.")
        self.assertEqual(record["queued_items"][1]["caster"], "caster1")
        self.assertEqual(record["dropped_items"][0]["commentary"], "Still waiting.")
        self.assertEqual(record["interrupted_current"]["commentary"], "Quiet for now.")
        self.assertTrue(current_non_event["interrupt_event"].is_set())
        self.assertIn("Focused context:", captured["user_prompt"])
        self.assertIn("Tactical facts:", captured["user_prompt"])
        self.assertIn("Event input:", captured["user_prompt"])
        self.assertIn('"previous_events"', captured["user_prompt"])
        self.assertIn('"tactical_facts"', captured["user_prompt"])

    def test_process_event_wrapper_splits_single_line_multi_sentence_event_output(self):
        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": "Yanni kills Tony. Triple for Yanni.",
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        wrapper = {
            "input": {
                "context": {
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Yanni", "team": "CT", "map_callout": "Short"}],
                },
                "previous_events": [],
                "current_events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Yanni", "team": "CT", "map_callout": "Short", "round_kills": 3},
                        "victim": {"name": "Tony", "team": "T"},
                    }
                ],
                "derived_tactical_summary": {"confidence": "medium"},
            }
        }

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=16, snapshot={})

        self.assertEqual(record["status"], "completed")
        self.assertEqual([item["tag"] for item in record["queued_items"]], ["event", "followup"])
        self.assertEqual(record["queued_items"][0]["commentary"], "Yanni kills Tony.")
        self.assertEqual(record["queued_items"][1]["commentary"], "Triple for Yanni.")

    def test_process_interval_wrapper_idle_color_keeps_three_lines_in_single_tts_item(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["user_prompt"] = user_prompt
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": "Mid is quiet.\nCT are spread thin.\nThis could turn fast.",
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion
        MODULE.INTERVAL_MODE_INDEX = 0

        wrapper = {
            "input": {
                "context": {
                    "bomb_state": "carried",
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Niko", "team": "T", "map_callout": "A Ramp"}],
                },
                "previous_events": [],
                "current_events": [],
                "derived_tactical_summary": {
                    "next_move_hint": "a_leaning",
                    "pressure": {"site": "a_leaning"},
                    "confidence": "medium",
                },
            }
        }

        record = MODULE.process_interval_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=14)

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["mode"], "idle_color")
        self.assertEqual(len(record["queued_items"]), 1)
        self.assertEqual(record["queued_items"][0]["caster"], "caster1")
        self.assertEqual(record["queued_items"][0]["tag"], "idle")
        self.assertEqual(
            record["queued_items"][0]["commentary"],
            "Mid is quiet. CT are spread thin. This could turn fast.",
        )
        self.assertIn("Live context:", captured["user_prompt"])
        self.assertNotIn('"previous_events"', captured["user_prompt"])
        self.assertIn('"tactical_facts"', captured["user_prompt"])

    def test_process_interval_wrapper_conversation_uses_chemistry_lines_without_model_call(self):
        def fail_request_chat_completion(config, system_prompt, user_prompt):
            raise AssertionError("idle_conversation should not call the text model")

        MODULE.request_chat_completion = fail_request_chat_completion
        MODULE.choose_chemistry_line_set = lambda: [
            {"caster": "caster1", "text": "They are slowing down."},
            {"caster": "caster0", "text": "That smoke changed the pace."},
            {"caster": "caster1", "text": "B might still be live."},
        ]
        MODULE.INTERVAL_MODE_INDEX = 1

        wrapper = {
            "input": {
                "context": {
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Niko", "team": "T", "map_callout": "A Ramp"}],
                },
                "previous_events": [],
                "current_events": [],
                "derived_tactical_summary": {
                    "next_move_hint": "b_leaning",
                    "pressure": {"site": "b_leaning"},
                    "confidence": "medium",
                },
            }
        }

        record = MODULE.process_interval_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=15)

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["mode"], "idle_conversation")
        self.assertEqual([item["caster"] for item in record["queued_items"]], ["caster1", "caster0", "caster1"])
        self.assertEqual([item["tag"] for item in record["queued_items"]], ["idle", "idle", "idle"])
        self.assertEqual(
            [item["commentary"] for item in record["queued_items"]],
            ["They are slowing down.", "That smoke changed the pace.", "B might still be live."],
        )
        self.assertNotIn("llm", record)


if __name__ == "__main__":
    unittest.main()
