import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("gsi_prompt_pipeline_v4.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("gsi_prompt_pipeline_v4", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GsiPromptPipelineV4Tests(unittest.TestCase):
    def test_should_log_prompt_wrapper_skips_chemistry(self):
        self.assertTrue(MODULE.should_log_prompt_wrapper("idle_analysis"))
        self.assertTrue(MODULE.should_log_prompt_wrapper("event_trigger"))
        self.assertFalse(MODULE.should_log_prompt_wrapper("chemistry"))

    def test_build_event_descriptions_formats_multi_kill_progression(self):
        events = [
            {
                "event_type": "kill",
                "killer": {"name": "Walt", "team": "CT", "round_kills": 1},
                "victim": {"name": "Uri", "team": "T"},
            },
            {
                "event_type": "kill",
                "killer": {"name": "Walt", "team": "CT", "round_kills": 3},
                "victim": {"name": "Tony", "team": "T"},
            },
            {
                "event_type": "kill",
                "killer": {"name": "Walt", "team": "CT", "round_kills": 5},
                "victim": {"name": "Moe", "team": "T"},
            },
        ]

        self.assertEqual(
            MODULE.build_event_descriptions(events),
            [
                "Walt kills Uri",
                "3 kills for Walt",
                "Ace for Walt",
            ],
        )

    def test_build_event_wrapper_uses_minimal_event_facing_data(self):
        filtered_batch = {
            "events": [
                {
                    "event_type": "kill",
                    "killer": {"name": "Walt", "team": "CT", "round_kills": 2},
                    "victim": {"name": "Uri", "team": "T"},
                }
            ]
        }
        MODULE.PIPELINE_STATE.event_analysis_toggle = 0

        wrapper = MODULE.build_event_wrapper(filtered_batch)

        self.assertEqual(
            wrapper,
            {
                "input": {
                    "event_descriptions": ["2 kills for Walt"],
                    "current_events": filtered_batch["events"],
                    "request": {"mode": "event_trigger", "output_count": 2},
                    "analysis_caster": "caster0",
                }
            },
        )

    def test_build_idle_wrapper_uses_player_locations_and_mode_request(self):
        snapshot = {
            "map": {
                "name": "de_dust2",
                "phase": "live",
                "round": 2,
                "team_ct": {"score": 1},
                "team_t": {"score": 0},
            },
            "round": {"phase": "live", "win_team": None},
            "player": {
                "name": "GrowthHormones",
                "team": "T",
                "position": "-720, -830, 140",
                "state": {"health": 100},
            },
        }

        self.assertEqual(
            MODULE.build_idle_wrapper(snapshot, "idle_analysis"),
            {
                "input": {
                    "score": {"CT": 1, "T": 0},
                    "player_locations": [
                        {"name": "GrowthHormones", "team": "T", "map_callout": "T Spawn"},
                    ],
                    "request": {"mode": "idle_analysis", "output_count": 3},
                }
            },
        )


if __name__ == "__main__":
    unittest.main()
