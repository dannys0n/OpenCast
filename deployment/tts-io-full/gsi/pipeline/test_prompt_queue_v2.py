import importlib.util
import json
import sys
import tempfile
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


if __name__ == "__main__":
    unittest.main()
