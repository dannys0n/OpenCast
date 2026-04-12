import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("gsi_prompt_pipeline_v3.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("gsi_prompt_pipeline_v3", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GsiPromptPipelineV3Tests(unittest.TestCase):
    def test_build_training_wrapper_adds_match_context_and_event_context(self):
        filtered_batch = {
            "created_at": "2026-04-12T07:30:00",
            "events": [
                {
                    "event_type": "kill",
                    "killer": {"name": "Walt", "team": "CT", "map_callout": "Top Mid"},
                    "victim": {"name": "Uri", "team": "T"},
                }
            ],
        }
        current_snapshot = {
            "map": {
                "name": "de_dust2",
                "phase": "live",
                "round": 12,
                "team_ct": {"score": 3},
                "team_t": {"score": 8},
            },
            "round": {
                "phase": "live",
                "win_team": None,
            },
        }
        previous_events = [
            {
                "event_type": "bomb_event",
                "state_after": "planted",
            }
        ]

        wrapper = MODULE.build_training_wrapper(
            filtered_batch,
            current_snapshot,
            payload_sequence=42,
            previous_events=previous_events,
        )

        self.assertEqual(
            wrapper,
            {
                "input": {
                    "match_context": {
                        "map_name": "de_dust2",
                        "map_phase": "live",
                        "round_phase": "live",
                        "round_number": 12,
                        "score": {"CT": 3, "T": 8},
                        "win_team": None,
                    },
                    "previous_events": previous_events,
                    "current_events": [
                        {
                            "event_type": "kill",
                            "killer": {"name": "Walt", "team": "CT", "map_callout": "Top Mid"},
                            "victim": {"name": "Uri", "team": "T"},
                        }
                    ],
                    "overrides": {
                        "caster": None,
                        "prompt_style": None,
                    },
                }
            },
        )

    def test_build_previous_events_summary_keeps_only_last_primary_event_slimmed_down(self):
        events = [
            {
                "event_type": "kill",
                "killer": {
                    "name": "Walt",
                    "team": "CT",
                    "map_callout": "Top Mid",
                    "round_kills": 2,
                    "kda": {"kills": 5, "deaths": 1, "assists": 0},
                },
                "victim": {"name": "Uri", "team": "T"},
            },
            {
                "event_type": "team_counter",
                "alive_counts_after": {"T": 4},
            },
        ]

        previous_events = MODULE.build_previous_events_summary(events)

        self.assertEqual(
            previous_events,
            [
                {
                    "event_type": "kill",
                    "killer": {"name": "Walt", "team": "CT", "map_callout": "Top Mid"},
                    "victim": {"name": "Uri", "team": "T"},
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
