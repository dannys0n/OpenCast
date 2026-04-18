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
    def test_build_training_wrapper_adds_tactical_summary_and_request(self):
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
                "bomb": "carried",
            },
            "allplayers": {
                "2": {
                    "name": "Walt",
                    "team": "CT",
                    "position": "-104, 386, 44",
                    "state": {"health": 100},
                },
                "3": {
                    "name": "Uri",
                    "team": "T",
                    "position": "-1522, 1930, 53",
                    "state": {"health": 100},
                },
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
                    "context": {
                        "bomb_state": "carried",
                        "score": {"CT": 3, "T": 8},
                        "alive_players": [
                            {"name": "Walt", "team": "CT", "map_callout": "Top Mid"},
                            {"name": "Uri", "team": "T", "map_callout": "B Car"},
                        ],
                    },
                    "previous_events": previous_events,
                    "current_events": [
                        {
                            "event_type": "kill",
                            "killer": {"name": "Walt", "team": "CT", "map_callout": "Top Mid"},
                            "victim": {"name": "Uri", "team": "T"},
                        }
                    ],
                    "derived_tactical_summary": {
                        "alive_counts": {
                            "ct": 1,
                            "t": 1,
                        },
                        "analysis_mode": "map_specific",
                        "confidence": "medium",
                        "isolated_player": "none",
                        "key_risk": "b_hit_readable",
                        "map_control": {
                            "cat": "empty",
                            "long": "empty",
                            "mid": "ct",
                        },
                        "next_move_hint": "b_leaning",
                        "pressure": {
                            "b": "medium",
                            "site": "b_leaning",
                        },
                        "position_data": "full",
                        "rotation_favor": "neutral",
                        "score_context": {
                            "leader": "t",
                            "margin": "clear",
                        },
                    },
                    "request": {
                        "mode": "event_bundle",
                        "lines": [
                            {"caster": "caster0", "style": "play_by_play_event"},
                            {"caster": "caster1", "style": "play_by_play_follow_up"},
                        ],
                    },
                },
                "output": {
                    "lines": ["", ""],
                },
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

    def test_build_match_context_adds_alive_players_from_local_player_when_allplayers_missing(self):
        snapshot = {
            "map": {
                "name": "de_dust2",
                "phase": "live",
                "round": 2,
                "team_ct": {"score": 1},
                "team_t": {"score": 0},
            },
            "round": {"phase": "live", "win_team": None, "bomb": "carried"},
            "player": {
                "name": "GrowthHormones",
                "team": "T",
                "position": "-720, -830, 140",
                "state": {"health": 100},
            },
        }

        self.assertEqual(
            MODULE.build_match_context(snapshot),
            {
                "map_name": "de_dust2",
                "map_phase": "live",
                "round_phase": "live",
                "round_number": 2,
                "bomb_state": "carried",
                "score": {"CT": 1, "T": 0},
                "win_team": None,
                "alive_players": [
                    {"name": "GrowthHormones", "team": "T", "map_callout": "T Spawn"},
                ],
            },
        )

    def test_build_idle_wrapper_omits_previous_events_and_bomb_state_from_summary(self):
        snapshot = {
            "map": {
                "name": "de_dust2",
                "phase": "live",
                "round": 2,
                "team_ct": {"score": 1},
                "team_t": {"score": 0},
            },
            "round": {"phase": "live", "win_team": None, "bomb": "carried"},
            "player": {
                "name": "GrowthHormones",
                "team": "T",
                "position": "-720, -830, 140",
                "state": {"health": 100},
            },
        }
        previous_events = [{"event_type": "bomb_event", "state_after": "planted"}]

        self.assertEqual(
            MODULE.build_idle_wrapper(snapshot, previous_events, "idle_conversation"),
            {
                "input": {
                    "context": {
                        "bomb_state": "carried",
                        "score": {"CT": 1, "T": 0},
                        "alive_players": [
                            {"name": "GrowthHormones", "team": "T", "map_callout": "T Spawn"},
                        ],
                    },
                    "previous_events": [],
                    "current_events": [],
                    "derived_tactical_summary": {
                        "alive_counts": {
                            "ct": 0,
                            "t": 1,
                        },
                        "analysis_mode": "map_specific",
                        "confidence": "low",
                        "isolated_player": "none",
                        "key_risk": "none",
                        "map_control": {
                            "cat": "empty",
                            "long": "empty",
                            "mid": "empty",
                        },
                        "next_move_hint": "unclear",
                        "pressure": {
                            "b": "low",
                            "site": "unclear",
                        },
                        "position_data": "full",
                        "rotation_favor": "neutral",
                        "score_context": {
                            "leader": "ct",
                            "margin": "close",
                        },
                    },
                    "request": {
                        "mode": "idle_conversation",
                        "lines": [
                            {"caster": "caster0", "style": "idle_color"},
                            {"caster": "caster1", "style": "idle_color"},
                            {"caster": "caster0", "style": "idle_color"},
                        ],
                    },
                },
                "output": {
                    "lines": ["", "", ""],
                },
            },
        )

    def test_build_training_wrapper_uses_generic_fallback_summary_for_unsupported_map(self):
        filtered_batch = {
            "created_at": "2026-04-12T07:30:00",
            "events": [],
        }
        current_snapshot = {
            "map": {
                "name": "de_mirage",
                "phase": "live",
                "round": 6,
                "team_ct": {"score": 2},
                "team_t": {"score": 3},
            },
            "round": {
                "phase": "live",
                "win_team": None,
                "bomb": "carried",
            },
            "allplayers": {
                "2": {
                    "name": "Walt",
                    "team": "CT",
                    "position": "-104, 386, 44",
                    "state": {"health": 100},
                },
                "3": {
                    "name": "Uri",
                    "team": "T",
                    "position": "-1522, 1930, 53",
                    "state": {"health": 100},
                },
            },
        }

        wrapper = MODULE.build_training_wrapper(
            filtered_batch,
            current_snapshot,
            payload_sequence=7,
            previous_events=[],
        )

        self.assertEqual(
            wrapper["input"]["derived_tactical_summary"],
            {
                "alive_counts": {"ct": 1, "t": 1},
                "analysis_mode": "generic",
                "confidence": "low",
                "key_risk": "none",
                "next_move_hint": "unclear",
                "position_data": "none",
                "score_context": {
                    "leader": "t",
                    "margin": "close",
                },
            },
        )
        self.assertEqual(wrapper["output"], {"lines": ["", ""]})

    def test_build_blank_output_matches_requested_line_count(self):
        self.assertEqual(MODULE.build_blank_output("event_bundle"), {"lines": ["", ""]})
        self.assertEqual(MODULE.build_blank_output("idle_color"), {"lines": ["", "", ""]})
        self.assertEqual(MODULE.build_blank_output("idle_conversation"), {"lines": ["", "", ""]})


if __name__ == "__main__":
    unittest.main()
