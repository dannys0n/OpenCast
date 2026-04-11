import importlib.util
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("prompt_queue_v2.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("prompt_queue_v2", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class PromptQueueV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)

        self.original_state_dir = MODULE.STATE_DIR
        self.original_runtime_history_path = MODULE.PROMPT_RUNTIME_HISTORY_PATH
        self.original_runtime_latest_path = MODULE.PROMPT_RUNTIME_LATEST_PATH
        self.original_legacy_history_path = MODULE.LEGACY_PROMPT_QUEUE_HISTORY_PATH
        self.original_legacy_latest_path = MODULE.LEGACY_PROMPT_QUEUE_LATEST_PATH
        self.original_legacy_state_path = MODULE.LEGACY_PROMPT_QUEUE_STATE_PATH
        self.original_request_chat_completion = MODULE.request_chat_completion
        self.original_build_text_llm_config = MODULE.build_text_llm_config
        self.original_build_tts_config = MODULE.build_tts_config
        self.original_stream_tts_sequence_playback = MODULE.stream_tts_sequence_playback
        self.original_tts_pending_playbacks = MODULE.TTS_PENDING_PLAYBACKS
        self.original_tts_next_submission_id = MODULE.TTS_NEXT_SUBMISSION_ID
        self.original_tts_next_playback_id = MODULE.TTS_NEXT_PLAYBACK_ID
        self.original_tts_worker_thread = MODULE.TTS_WORKER_THREAD

        MODULE.STATE_DIR = self.state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.state_dir / "prompt_runtime_pretty.jsonl"
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.state_dir / "prompt_runtime_latest.json"
        MODULE.LEGACY_PROMPT_QUEUE_HISTORY_PATH = self.state_dir / "prompt_queue_pretty.jsonl"
        MODULE.LEGACY_PROMPT_QUEUE_LATEST_PATH = self.state_dir / "prompt_queue_latest.json"
        MODULE.LEGACY_PROMPT_QUEUE_STATE_PATH = self.state_dir / "prompt_queue_state.json"

    def tearDown(self):
        MODULE.STATE_DIR = self.original_state_dir
        MODULE.PROMPT_RUNTIME_HISTORY_PATH = self.original_runtime_history_path
        MODULE.PROMPT_RUNTIME_LATEST_PATH = self.original_runtime_latest_path
        MODULE.LEGACY_PROMPT_QUEUE_HISTORY_PATH = self.original_legacy_history_path
        MODULE.LEGACY_PROMPT_QUEUE_LATEST_PATH = self.original_legacy_latest_path
        MODULE.LEGACY_PROMPT_QUEUE_STATE_PATH = self.original_legacy_state_path
        MODULE.request_chat_completion = self.original_request_chat_completion
        MODULE.build_text_llm_config = self.original_build_text_llm_config
        MODULE.build_tts_config = self.original_build_tts_config
        MODULE.stream_tts_sequence_playback = self.original_stream_tts_sequence_playback
        MODULE.TTS_PENDING_PLAYBACKS = self.original_tts_pending_playbacks
        MODULE.TTS_NEXT_SUBMISSION_ID = self.original_tts_next_submission_id
        MODULE.TTS_NEXT_PLAYBACK_ID = self.original_tts_next_playback_id
        MODULE.TTS_WORKER_THREAD = self.original_tts_worker_thread
        self.temp_dir.cleanup()

    def test_process_filtered_batch_builds_instruction_snapshot_and_immediate_playback(self):
        captured = {}

        def fake_build_text_llm_config(repo_root):
            captured["repo_root"] = str(repo_root)
            return type(
                "FakeTextConfig",
                (),
                {
                    "model_name": "fake-model",
                    "temperature": 0.4,
                    "max_tokens": 160,
                    "timeout_seconds": 45.0,
                },
            )()

        def fake_build_tts_config(repo_root):
            captured["tts_repo_root"] = str(repo_root)
            return type(
                "FakeTtsConfig",
                (),
                {
                    "voice_name": "clone:test_voice",
                    "sample_rate": 24000,
                    "timeout_seconds": 120.0,
                    "api_base": "http://127.0.0.1:8880",
                    "model": "tts-1",
                },
            )()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            captured["system_prompt"] = system_prompt
            captured["user_prompt"] = user_prompt
            return {
                "request": {
                    "model": config.model_name,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                "response": {},
                "raw_text": "Big opener.\nClean trade.\nBomb down.",
            }

        def fake_stream_tts_sequence_playback(config, tts_prompts):
            captured["tts_prompts"] = tts_prompts
            return {
                "line_count": len(tts_prompts),
                "speed": tts_prompts[0]["speed"],
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.build_tts_config = fake_build_tts_config
        MODULE.request_chat_completion = fake_request_chat_completion
        MODULE.stream_tts_sequence_playback = fake_stream_tts_sequence_playback

        MODULE.reset_prompt_runtime_state()

        filtered_batch = {
            "created_at": "2026-04-10T00:00:00",
            "payload_sequence": 12,
            "trigger_paths": ["allplayers.*.match_stats.kills"],
            "events": [
                {
                    "event_type": "kill",
                    "killer": {"name": "Uri", "team": "T"},
                    "victim": {"name": "Maru", "team": "CT"},
                }
            ],
        }

        record = MODULE.process_filtered_batch(filtered_batch, repo_root=Path("/tmp/opencast"))

        self.assertEqual(record["status"], "completed")
        self.assertEqual(record["payload_sequence"], 12)
        self.assertEqual(
            record["prompt_schema"]["gameplay_snapshot"],
            {
                "created_at": record["prompt_schema"]["gameplay_snapshot"]["created_at"],
                "payload_sequence": 12,
                "trigger_paths": ["allplayers.*.match_stats.kills"],
                "events": [
                    {
                        "event_type": "kill",
                        "killer": {"name": "Uri", "team": "T"},
                        "victim": {"name": "Maru", "team": "CT"},
                    }
                ],
            },
        )
        self.assertEqual(
            record["llm"]["commentary_text"],
            "Big opener.",
        )
        self.assertEqual(record["tts"]["line_count"], 1)
        self.assertEqual(record["tts"]["commentary_text"], "Big opener.")
        self.assertEqual(record["tts"]["submission_id"], 1)
        self.assertIn("No thinking.", record["prompt_schema"]["instruction"])
        self.assertIn("Return only one short sentence as plain text.", record["prompt_schema"]["instruction"])
        self.assertIn("No JSON.", record["prompt_schema"]["instruction"])
        self.assertIn("Never mention entity ids", record["prompt_schema"]["instruction"])
        self.assertIn("Gameplay snapshot:", captured["user_prompt"])
        self.assertIn('"payload_sequence": 12', captured["user_prompt"])
        self.assertNotIn('"entity_id"', captured["user_prompt"])
        self.assertNotIn('"association"', captured["user_prompt"])
        self.assertNotIn('"players"', captured["user_prompt"])
        self.assertEqual(len(captured["tts_prompts"]), 1)
        self.assertEqual(captured["tts_prompts"][0]["voice_name"], "clone:test_voice")
        self.assertEqual(captured["tts_prompts"][0]["caster"], MODULE.PROMPT_TTS_CASTER)
        self.assertEqual(captured["tts_prompts"][0]["emotion"], MODULE.PROMPT_TTS_EMOTION)

        latest_record = json.loads(MODULE.PROMPT_RUNTIME_LATEST_PATH.read_text())
        self.assertEqual(latest_record["status"], "completed")
        self.assertEqual(latest_record["payload_sequence"], 12)

        history_text = MODULE.PROMPT_RUNTIME_HISTORY_PATH.read_text()
        self.assertIn('"status": "completed"', history_text)
        self.assertIn('"payload_sequence": 12', history_text)

    def test_tts_playback_stays_in_event_order_when_llm_finishes_out_of_order(self):
        captured = {
            "llm_started": [],
            "tts_commentary_order": [],
        }

        first_prompt_released = threading.Event()

        def fake_build_text_llm_config(repo_root):
            return type(
                "FakeTextConfig",
                (),
                {
                    "model_name": "fake-model",
                    "temperature": 0.4,
                    "max_tokens": 160,
                    "timeout_seconds": 45.0,
                },
            )()

        def fake_build_tts_config(repo_root):
            return type(
                "FakeTtsConfig",
                (),
                {
                    "voice_name": "clone:test_voice",
                    "sample_rate": 24000,
                    "timeout_seconds": 120.0,
                    "api_base": "http://127.0.0.1:8880",
                    "model": "tts-1",
                },
            )()

        def fake_request_chat_completion(config, system_prompt, user_prompt):
            payload_sequence = 0
            for line in user_prompt.splitlines():
                if '"payload_sequence":' in line:
                    payload_sequence = int(line.split(":", 1)[1].strip().rstrip(","))
                    break

            captured["llm_started"].append(payload_sequence)
            if payload_sequence == 10:
                first_prompt_released.wait(timeout=2.0)
            return {
                "request": {
                    "model": config.model_name,
                },
                "response": {},
                "raw_text": f"Call for {payload_sequence}.",
            }

        def fake_stream_tts_sequence_playback(config, tts_prompts):
            commentary = tts_prompts[0]["commentary"]
            captured["tts_commentary_order"].append(commentary)
            time.sleep(0.01)
            return {
                "line_count": len(tts_prompts),
                "speed": tts_prompts[0]["speed"],
            }

        MODULE.build_text_llm_config = fake_build_text_llm_config
        MODULE.build_tts_config = fake_build_tts_config
        MODULE.request_chat_completion = fake_request_chat_completion
        MODULE.stream_tts_sequence_playback = fake_stream_tts_sequence_playback

        MODULE.reset_prompt_runtime_state()

        results = {}

        def run_batch(batch_key, filtered_batch):
            results[batch_key] = MODULE.process_filtered_batch(filtered_batch, repo_root=Path("/tmp/opencast"))

        first_batch = {
            "created_at": "2026-04-10T00:00:00",
            "payload_sequence": 10,
            "trigger_paths": ["round.phase"],
            "events": [
                {
                    "event_type": "round_result",
                    "winner": "T",
                }
            ],
        }
        second_batch = {
            "created_at": "2026-04-10T00:00:01",
            "payload_sequence": 11,
            "trigger_paths": ["allplayers.*.match_stats.kills"],
            "events": [
                {
                    "event_type": "kill",
                    "killer": {"name": "Uri", "team": "T"},
                    "victim": {"name": "Maru", "team": "CT"},
                }
            ],
        }

        first_thread = threading.Thread(target=run_batch, args=("first", first_batch))
        second_thread = threading.Thread(target=run_batch, args=("second", second_batch))

        first_thread.start()
        time.sleep(0.02)
        second_thread.start()
        time.sleep(0.05)
        first_prompt_released.set()

        first_thread.join()
        second_thread.join()

        self.assertEqual(captured["llm_started"], [10, 11])
        self.assertEqual(
            captured["tts_commentary_order"],
            ["Call for 10.", "Call for 11."],
        )
        self.assertEqual(results["first"]["tts"]["submission_id"], 1)
        self.assertEqual(results["second"]["tts"]["submission_id"], 2)
        self.assertEqual(results["first"]["status"], "completed")
        self.assertEqual(results["second"]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
