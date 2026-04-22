import importlib.util
import sys
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("gsi_prompt_pipeline_v5.py")
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("gsi_prompt_pipeline_v5", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class GsiPromptPipelineV5Tests(unittest.TestCase):
    def setUp(self):
        MODULE.PIPELINE_STATE.event_followup_toggle = 0
        MODULE.PIPELINE_STATE.bomb_planted_round_key = None

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

    def test_next_event_followup_caster_alternates_between_casters(self):
        self.assertEqual(MODULE.next_event_followup_caster(), "caster1")
        self.assertEqual(MODULE.next_event_followup_caster(), "caster0")
        self.assertEqual(MODULE.next_event_followup_caster(), "caster1")

    def test_prompting_is_ready_when_phase_countdown_is_present_and_numeric(self):
        self.assertTrue(
            MODULE.prompting_is_ready(
                {"phase_countdowns": {"phase_ends_in": "82.3"}}
            )
        )
        self.assertTrue(
            MODULE.prompting_is_ready(
                {"phase_countdowns": {"phase_ends_in": "0"}}
            )
        )
        self.assertFalse(
            MODULE.prompting_is_ready(
                {"phase_countdowns": {"phase_ends_in": ""}}
            )
        )
        self.assertFalse(
            MODULE.prompting_is_ready(
                {"phase_countdowns": {"phase_ends_in": "not-a-number"}}
            )
        )
        self.assertFalse(MODULE.prompting_is_ready({"phase_countdowns": {}}))

    def test_prompting_became_invalid_when_phase_countdown_drops_out(self):
        self.assertTrue(
            MODULE.prompting_became_invalid(
                {"phase_countdowns": {"phase_ends_in": "12.7"}},
                {"phase_countdowns": {"phase_ends_in": ""}},
            )
        )
        self.assertFalse(
            MODULE.prompting_became_invalid(
                {"phase_countdowns": {"phase_ends_in": ""}},
                {"phase_countdowns": {"phase_ends_in": "12.7"}},
            )
        )

    def test_should_bootstrap_prompting_from_event_when_entering_valid_map(self):
        previous_snapshot = {
            "map": {"name": ""},
            "phase_countdowns": {"phase_ends_in": ""},
        }
        current_snapshot = {
            "map": {"name": "de_dust2"},
            "phase_countdowns": {"phase_ends_in": ""},
        }
        payload = {
            "previously": {
                "player": {
                    "state": {
                        "round_kills": 0,
                    }
                }
            }
        }

        self.assertTrue(
            MODULE.should_bootstrap_prompting_from_event(
                previous_snapshot,
                current_snapshot,
                payload,
            )
        )

    def test_should_not_bootstrap_prompting_without_raw_gsi_activity_or_valid_map(self):
        previous_snapshot = {
            "map": {"name": ""},
            "phase_countdowns": {"phase_ends_in": ""},
        }

        self.assertFalse(
            MODULE.should_bootstrap_prompting_from_event(
                previous_snapshot,
                {"map": {"name": "de_dust2"}, "phase_countdowns": {"phase_ends_in": ""}},
                {},
            )
        )
        self.assertFalse(
            MODULE.should_bootstrap_prompting_from_event(
                previous_snapshot,
                {"map": {"name": ""}, "phase_countdowns": {"phase_ends_in": ""}},
                {"previously": {"player": {"state": {"round_kills": 0}}}},
            )
        )

    def test_build_request_idle_color_alternates_casters(self):
        self.assertEqual(
            MODULE.build_request("idle_color"),
            {
                "mode": "idle_color",
                "lines": [
                    {"caster": "caster1", "style": "idle_color"},
                    {"caster": "caster0", "style": "idle_color"},
                    {"caster": "caster1", "style": "idle_color"},
                ],
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

    def test_build_match_context_omits_alive_players(self):
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
                "local_player": {
                    "name": "GrowthHormones",
                    "team": "T",
                    "map_callout": "T Spawn",
                    "health": 100,
                },
            },
        )

    def test_build_idle_wrapper_omits_previous_events_and_uses_empty_alive_player_context_without_allplayers(self):
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
                        "local_player": {
                            "name": "GrowthHormones",
                            "team": "T",
                            "map_callout": "T Spawn",
                            "health": 100,
                        },
                    },
                    "previous_events": [],
                    "current_events": [],
                    "derived_tactical_summary": {
                        "analysis_mode": "map_specific",
                        "score_context": {
                            "leader": "ct",
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

    def test_has_full_player_context_requires_allplayers(self):
        self.assertFalse(
            MODULE.has_full_player_context(
                {
                    "player": {
                        "name": "GrowthHormones",
                        "team": "T",
                        "state": {"health": 100},
                    }
                }
            )
        )
        self.assertTrue(
            MODULE.has_full_player_context(
                {
                    "allplayers": {
                        "2": {
                            "name": "Walt",
                            "team": "CT",
                            "state": {"health": 100},
                        }
                    }
                }
            )
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
                },
            },
        )
        self.assertEqual(wrapper["output"], {"lines": ["", ""]})

    def test_build_blank_output_matches_requested_line_count(self):
        self.assertEqual(MODULE.build_blank_output("event_bundle"), {"lines": ["", ""]})
        self.assertEqual(MODULE.build_blank_output("idle_color"), {"lines": ["", "", ""]})
        self.assertEqual(MODULE.build_blank_output("idle_conversation"), {"lines": ["", "", ""]})

    def test_filter_duplicate_round_bomb_plants_drops_duplicate_plant_in_same_round(self):
        snapshot = {
            "map": {
                "name": "de_dust2",
                "round": 12,
            }
        }
        first_batch = {
            "events": [
                {"event_type": "bomb_event", "state_after": "planted"},
                {"event_type": "kill", "killer": {"name": "Walt"}, "victim": {"name": "Uri"}},
            ]
        }
        second_batch = {
            "events": [
                {"event_type": "bomb_event", "state_after": "planted"},
                {"event_type": "kill", "killer": {"name": "Bread"}, "victim": {"name": "Felix"}},
            ]
        }

        filtered_first = MODULE.filter_duplicate_round_bomb_plants(first_batch, snapshot)
        filtered_second = MODULE.filter_duplicate_round_bomb_plants(second_batch, snapshot)

        self.assertEqual(len(filtered_first["events"]), 2)
        self.assertEqual(
            filtered_second["events"],
            [{"event_type": "kill", "killer": {"name": "Bread"}, "victim": {"name": "Felix"}}],
        )

    def test_filter_duplicate_round_bomb_plants_allows_plant_in_new_round(self):
        round_twelve_snapshot = {
            "map": {
                "name": "de_dust2",
                "round": 12,
            }
        }
        round_thirteen_snapshot = {
            "map": {
                "name": "de_dust2",
                "round": 13,
            }
        }
        batch = {
            "events": [
                {"event_type": "bomb_event", "state_after": "planted"},
            ]
        }

        filtered_first = MODULE.filter_duplicate_round_bomb_plants(batch, round_twelve_snapshot)
        filtered_second = MODULE.filter_duplicate_round_bomb_plants(batch, round_thirteen_snapshot)

        self.assertEqual(filtered_first["events"], [{"event_type": "bomb_event", "state_after": "planted"}])
        self.assertEqual(filtered_second["events"], [{"event_type": "bomb_event", "state_after": "planted"}])


if __name__ == "__main__":
    unittest.main()
