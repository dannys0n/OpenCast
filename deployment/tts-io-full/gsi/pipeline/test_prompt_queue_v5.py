import importlib.util
import json
import sys
import tempfile
import threading
import unittest
from collections import deque
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prompt_queue_v5.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("prompt_queue_v5", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PromptQueueV5Tests(unittest.TestCase):
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
        self.original_start_prefetch_for_item = MODULE.start_prefetch_for_item
        self.original_ensure_head_prefetch = MODULE.ensure_head_prefetch

        MODULE.STATE_DIR = self.state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.state_dir / "prompt_runtime_pretty.jsonl"
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.state_dir / "prompt_runtime_latest.json"
        MODULE.PROMPT_QUEUE_STATE_PATH = self.state_dir / "prompt_queue_state.json"
        MODULE.ensure_queue_worker = lambda repo_root: None
        MODULE.ensure_head_prefetch = lambda repo_root, *, tts_config=None: False
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
        MODULE.start_prefetch_for_item = self.original_start_prefetch_for_item
        MODULE.ensure_head_prefetch = self.original_ensure_head_prefetch
        self.temp_dir.cleanup()

    def test_extract_commentary_lines_prefers_clean_lines(self):
        raw_text = "1. Colin drops Maru.\n2. CT close the round."
        self.assertEqual(
            MODULE.extract_commentary_lines(raw_text, expected_max=2),
            ["Colin drops Maru.", "CT close the round."],
        )

    def test_extract_commentary_lines_supports_structured_json_lines(self):
        raw_text = json.dumps(
            {
                "lines": [
                    "Niko doubles up.",
                    "That opens A. Rotation is late.",
                ]
            }
        )
        self.assertEqual(
            MODULE.extract_commentary_lines(raw_text, expected_max=2),
            ["Niko doubles up.", "That opens A. Rotation is late."],
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

    def test_should_ignore_grenade_event_when_grenade_event_is_currently_in_tts(self):
        MODULE.CURRENT_PLAYBACK = {
            "id": 10,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Smoke blooms.",
            "payload_sequence": 7,
            "source": "event",
            "event_family": "grenade",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        wrapper = {
            "input": {
                "current_events": [
                    {
                        "event_type": "grenade_detonated",
                        "grenade_type": "flashbang",
                    }
                ]
            }
        }

        self.assertTrue(MODULE.should_ignore_event_prompt(wrapper, {}))

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

        dropped, interrupted_current, kill_counts, event_types = MODULE.prepare_queue_for_event_trigger()

        self.assertFalse(current_non_event["interrupt_event"].is_set())
        self.assertIsNone(interrupted_current)
        self.assertEqual([item["id"] for item in dropped], [11])
        self.assertEqual(kill_counts, {"ct": 0, "t": 0})
        self.assertEqual(event_types, [])
        self.assertEqual(list(MODULE.PLAYBACK_QUEUE), [])

    def test_prepare_queue_for_event_trigger_keeps_queued_event_items(self):
        current_event = {
            "id": 10,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Opening frag.",
            "payload_sequence": 20,
            "source": "event",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        queued_kill_event = {
            "id": 11,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Second frag.",
            "payload_sequence": 21,
            "source": "event",
            "event_family": "kill",
            "kill_counts": {"ct": 1, "t": 0},
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        queued_kill_followup = {
            "id": 12,
            "tag": "followup",
            "caster": "caster1",
            "prompt_style": "play_by_play_follow_up",
            "commentary": "That opens the site.",
            "payload_sequence": 21,
            "source": "event",
            "event_family": "kill",
            "kill_counts": {"ct": 1, "t": 0},
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        queued_grenade_event = {
            "id": 13,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Flash in.",
            "payload_sequence": 22,
            "source": "event",
            "event_family": "grenade",
            "kill_counts": {"ct": 0, "t": 0},
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        kept_bomb_event = {
            "id": 14,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Bomb goes down.",
            "payload_sequence": 23,
            "source": "event",
            "event_family": "other",
            "event_types": ["bomb_event"],
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.CURRENT_PLAYBACK = current_event
        MODULE.PLAYBACK_QUEUE.extend(
            [queued_kill_event, queued_kill_followup, queued_grenade_event, kept_bomb_event]
        )

        dropped, interrupted_current, kill_counts, event_types = MODULE.prepare_queue_for_event_trigger(
            [
                {
                    "event_type": "kill",
                    "killer": {"team": "T"},
                }
            ],
        )

        self.assertIsNone(interrupted_current)
        self.assertEqual([item["id"] for item in dropped], [12])
        self.assertEqual(kill_counts, {"ct": 0, "t": 1})
        self.assertEqual(event_types, ["kill"])
        self.assertEqual([item["id"] for item in MODULE.PLAYBACK_QUEUE], [11, 13, 14])

    def test_prepare_queue_for_event_trigger_cancels_prefetch_for_trimmed_item(self):
        prefetched_item = {
            "id": 21,
            "tag": "followup",
            "caster": "caster1",
            "prompt_style": "play_by_play_follow_up",
            "commentary": "That opens the site.",
            "payload_sequence": 41,
            "source": "event",
            "event_family": "kill",
            "kill_counts": {"ct": 1, "t": 0},
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
            "prefetch_started": True,
            "prefetch_cleanup_pending": False,
            "prefetch_cancel_event": threading.Event(),
        }
        kept_bomb_event = {
            "id": 22,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Bomb goes down.",
            "payload_sequence": 42,
            "source": "event",
            "event_family": "other",
            "event_types": ["bomb_event"],
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
            "prefetch_started": False,
            "prefetch_cleanup_pending": False,
        }
        MODULE.CURRENT_PLAYBACK = {
            "id": 20,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Opening frag.",
            "payload_sequence": 40,
            "source": "event",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.PLAYBACK_QUEUE.extend([prefetched_item, kept_bomb_event])

        MODULE.prepare_queue_for_event_trigger([{"event_type": "kill", "killer": {"team": "T"}}])

        self.assertTrue(prefetched_item["prefetch_cancel_event"].is_set())

    def test_enqueue_prompt_items_starts_prefetch_for_head_of_queue(self):
        started = []

        def fake_start_prefetch_for_item(item, repo_root, *, tts_config=None):
            started.append((item["id"], str(repo_root)))
            item["prefetch_started"] = True
            return True

        MODULE.start_prefetch_for_item = fake_start_prefetch_for_item
        MODULE.ensure_head_prefetch = self.original_ensure_head_prefetch

        first_item = MODULE.build_queue_item(
            commentary="First sentence.",
            caster="caster0",
            prompt_style="play_by_play_event",
            tag="event",
            payload_sequence=50,
            source="event",
        )
        second_item = MODULE.build_queue_item(
            commentary="Second sentence.",
            caster="caster1",
            prompt_style="play_by_play_follow_up",
            tag="followup",
            payload_sequence=50,
            source="event",
        )

        MODULE.enqueue_prompt_items([first_item, second_item], Path("/tmp/opencast"))

        self.assertEqual(started, [(first_item["id"], "/tmp/opencast")])

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
        self.assertNotIn("interrupted_current", record)
        self.assertFalse(current_non_event["interrupt_event"].is_set())
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

    def test_process_event_wrapper_supports_structured_json_output_and_still_splits_by_sentence(self):
        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": json.dumps(
                    {
                        "lines": [
                            "Yanni kills Tony.",
                            "Triple for Yanni. A is open now.",
                        ]
                    }
                ),
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

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=17, snapshot={})

        self.assertEqual(record["status"], "completed")
        self.assertEqual(
            [item["commentary"] for item in record["queued_items"]],
            ["Yanni kills Tony.", "Triple for Yanni.", "A is open now."],
        )
        self.assertEqual(
            [item["tag"] for item in record["queued_items"]],
            ["event", "followup", "followup"],
        )

    def test_process_event_wrapper_uses_followup_caster_from_request(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": "Bread kills Felix.\nThat keeps Long under pressure.",
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        wrapper = {
            "input": {
                "context": {
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Bread", "team": "CT", "map_callout": "Long"}],
                },
                "previous_events": [],
                "current_events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Bread", "team": "CT", "map_callout": "Long"},
                        "victim": {"name": "Felix", "team": "T"},
                    }
                ],
                "derived_tactical_summary": {"confidence": "medium"},
                "request": {
                    "mode": "event_bundle",
                    "lines": [
                        {"caster": "caster0", "style": "play_by_play_event"},
                        {"caster": "caster0", "style": "play_by_play_follow_up"},
                    ],
                },
            }
        }

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=18, snapshot={})

        self.assertEqual(record["status"], "completed")
        self.assertEqual([item["caster"] for item in record["queued_items"]], ["caster0", "caster0"])
        self.assertIn("Line 2: short caster0 follow-up line", captured["user_prompt"])

    def test_process_event_wrapper_keeps_original_current_events_in_prompt(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["called"] = True
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": "Fresh frag.\nThat opens space.",
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.request_chat_completion = fake_request_chat_completion

        current_event = {
            "id": 10,
            "tag": "event",
            "caster": "caster0",
            "prompt_style": "play_by_play_event",
            "commentary": "Opening frag.",
            "payload_sequence": 40,
            "source": "event",
            "interrupt_event": threading.Event(),
            "done_event": threading.Event(),
        }
        MODULE.CURRENT_PLAYBACK = current_event

        wrapper = {
            "input": {
                "context": {
                    "score": {"CT": 5, "T": 7},
                    "alive_players": [{"name": "Ropz", "team": "T", "map_callout": "Mid"}],
                },
                "previous_events": [],
                "current_events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Ropz", "team": "T", "map_callout": "Mid"},
                        "victim": {"name": "Yanni", "team": "CT"},
                    }
                ],
                "derived_tactical_summary": {"confidence": "medium"},
            }
        }

        record = MODULE.process_event_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=41, snapshot={})

        self.assertEqual(record["status"], "completed")
        self.assertTrue(captured.get("called"))
        self.assertEqual(record["llm"]["lines"], ["Fresh frag.", "That opens space."])
        self.assertEqual([item["commentary"] for item in record["queued_items"]], ["Fresh frag.", "That opens space."])
        self.assertNotIn("compacted_combat_backlog", record)

    def test_process_interval_wrapper_idle_color_queues_each_sentence_separately(self):
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
        self.assertEqual(len(record["queued_items"]), 3)
        self.assertEqual([item["caster"] for item in record["queued_items"]], ["caster1", "caster0", "caster1"])
        self.assertEqual([item["tag"] for item in record["queued_items"]], ["idle", "idle", "idle"])
        self.assertEqual(
            [item["commentary"] for item in record["queued_items"]],
            ["Mid is quiet.", "CT are spread thin.", "This could turn fast."],
        )
        self.assertIn("Live context:", captured["user_prompt"])
        self.assertIn("Requested caster order: caster1, caster0, caster1", captured["user_prompt"])
        self.assertNotIn('"previous_events"', captured["user_prompt"])
        self.assertIn('"tactical_facts"', captured["user_prompt"])

    def test_process_interval_wrapper_idle_color_supports_structured_json_output(self):
        def fake_build_text_llm_config(repo_root):
            return type("FakeTextConfig", (), {})()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            return {
                "request": {"model": "fake-model"},
                "response": {},
                "raw_text": json.dumps(
                    {
                        "lines": [
                            "Mid is quiet.",
                            "CT are spread thin. This could turn fast.",
                        ]
                    }
                ),
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

        record = MODULE.process_interval_wrapper(wrapper, Path("/tmp/opencast"), payload_sequence=18)

        self.assertEqual(record["status"], "completed")
        self.assertEqual(
            [item["commentary"] for item in record["queued_items"]],
            ["Mid is quiet.", "CT are spread thin.", "This could turn fast."],
        )
        self.assertEqual(
            [item["caster"] for item in record["queued_items"]],
            ["caster1", "caster0", "caster0"],
        )

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
