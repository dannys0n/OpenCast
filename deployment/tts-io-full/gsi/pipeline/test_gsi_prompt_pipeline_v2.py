import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("gsi_prompt_pipeline_v2.py")
SPEC = importlib.util.spec_from_file_location("gsi_prompt_pipeline_v2", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_player(name, team, health, round_kills=0, match_kills=0):
    return {
        "name": name,
        "steamid": f"steam-{name}",
        "team": team,
        "activity": "playing",
        "state": {
            "health": health,
            "armor": 100,
            "round_kills": round_kills,
        },
        "match_stats": {
            "kills": match_kills,
            "deaths": 0,
            "assists": 0,
            "score": 0,
        },
    }


def make_snapshot(
    *,
    round_phase="live",
    win_team=None,
    ct_score=0,
    t_score=0,
    round_number=1,
    allplayers=None,
    player=None,
    grenades=None,
    allgrenades=None,
):
    allplayers = allplayers or {}
    first_player = player if player is not None else next(iter(allplayers.values()), {})
    return {
        "map": {
            "name": "de_dust2",
            "mode": "competitive",
            "phase": "live",
            "round": round_number,
            "team_ct": {"score": ct_score},
            "team_t": {"score": t_score},
        },
        "round": {
            "phase": round_phase,
            "win_team": win_team,
        },
        "player": first_player,
        "allplayers": allplayers,
        "grenades": grenades or {},
        "allgrenades": allgrenades or {},
    }


class FilterImportantEventsTests(unittest.TestCase):
    def test_pairs_killer_and_victim_and_updates_team_counter(self):
        previous = make_snapshot(
            round_number=9,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=0, match_kills=8),
                "20": make_player("Bob", "T", 100, round_kills=0, match_kills=5),
            },
        )
        current = make_snapshot(
            round_number=9,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=1, match_kills=9),
                "20": make_player("Bob", "T", 0, round_kills=0, match_kills=5),
            },
        )

        filtered = MODULE.filter_important_events(previous, current, payload_sequence=2)

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["kill", "team_counter"])

        kill_event = filtered["events"][0]
        self.assertEqual(kill_event["killer"]["name"], "Alice")
        self.assertEqual(kill_event["victim"]["name"], "Bob")
        self.assertNotIn("association", kill_event)
        self.assertNotIn("entity_id", kill_event["killer"])
        self.assertEqual(
            kill_event["killer"],
            {
                "name": "Alice",
                "team": "CT",
            },
        )
        self.assertEqual(
            kill_event["victim"],
            {
                "name": "Bob",
                "team": "T",
            },
        )
        self.assertNotIn("transition_context", filtered)
        self.assertNotIn("snapshot_summary", filtered)

        team_counter_event = filtered["events"][1]
        self.assertEqual(
            team_counter_event["alive_counts_after"],
            {"T": 0},
        )

    def test_detects_round_end_and_round_win_increment(self):
        previous = make_snapshot(
            round_phase="live",
            win_team=None,
            ct_score=5,
            t_score=4,
            round_number=12,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=0, match_kills=8),
                "20": make_player("Bob", "T", 0, round_kills=0, match_kills=5),
            },
        )
        current = make_snapshot(
            round_phase="over",
            win_team="CT",
            ct_score=6,
            t_score=4,
            round_number=12,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=0, match_kills=8),
                "20": make_player("Bob", "T", 0, round_kills=0, match_kills=5),
            },
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=3,
            payload={
                "added": {
                    "round": {"win_team": "CT"},
                    "map": {"team_ct": {"score": 6}},
                }
            },
        )

        self.assertEqual(
            [event["event_type"] for event in filtered["events"]],
            ["round_result"],
        )
        self.assertEqual(filtered["events"][0]["winner"], "CT")
        self.assertEqual(filtered["events"][0]["winner_score"], 6)
        self.assertEqual(
            filtered["trigger_paths"],
            ["map.team_ct.score", "round.win_team"],
        )

    def test_detects_live_grenade_entity_but_not_equipped_grenade(self):
        player_with_equipped_grenade = make_player("Alice", "CT", 100, round_kills=0, match_kills=8)
        player_with_equipped_grenade["weapons"] = {
            "weapon_2": {
                "name": "weapon_hegrenade",
                "type": "Grenade",
                "state": "holstered",
            }
        }

        previous = make_snapshot(
            round_number=9,
            player=player_with_equipped_grenade,
            allplayers={
                "10": player_with_equipped_grenade,
            },
        )
        current = make_snapshot(
            round_number=9,
            player=player_with_equipped_grenade,
            allplayers={
                "10": player_with_equipped_grenade,
            },
            grenades={
                "501": {
                    "owner": "10",
                    "type": "frag",
                    "position": "120.0, 45.0, 5.0",
                    "velocity": "500.0, 10.0, 20.0",
                    "lifetime": "0.125",
                }
            },
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=4,
            payload={
                "added": {
                    "grenades": {
                        "501": {
                            "owner": "10",
                            "type": "frag",
                            "position": "120.0, 45.0, 5.0",
                        }
                    }
                }
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["grenade_thrown"])
        grenade_event = filtered["events"][0]
        self.assertEqual(grenade_event["owner_player"]["name"], "Alice")
        self.assertEqual(grenade_event["grenade_type"], "frag")
        self.assertNotIn("grenade", grenade_event)
        self.assertEqual(filtered["trigger_paths"], ["grenades.*.owner", "grenades.*.position", "grenades.*.type"])

    def test_ambiguous_multi_actor_kills_become_cluster_instead_of_guessed_pairs(self):
        previous = make_snapshot(
            round_number=9,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=0, match_kills=8),
                "11": make_player("Carol", "CT", 100, round_kills=0, match_kills=4),
                "20": make_player("Bob", "T", 100, round_kills=0, match_kills=5),
                "21": make_player("Dave", "T", 100, round_kills=0, match_kills=3),
            },
        )
        current = make_snapshot(
            round_number=9,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=1, match_kills=9),
                "11": make_player("Carol", "CT", 100, round_kills=1, match_kills=5),
                "20": make_player("Bob", "T", 0, round_kills=0, match_kills=5),
                "21": make_player("Dave", "T", 0, round_kills=0, match_kills=3),
            },
        )

        filtered = MODULE.filter_important_events(previous, current, payload_sequence=5)

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["kill_cluster", "team_counter"])
        cluster = filtered["events"][0]
        self.assertEqual(cluster["kill_count"], 2)
        self.assertEqual(sorted(k["name"] for k in cluster["killers"]), ["Alice", "Carol"])
        self.assertEqual(sorted(v["name"] for v in cluster["victims"]), ["Bob", "Dave"])

    def test_local_player_only_session_emits_kill_without_allplayers(self):
        previous = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "T", 100, round_kills=0, match_kills=1),
            allplayers={},
        )
        current = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "T", 100, round_kills=1, match_kills=2),
            allplayers={},
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=7,
            payload={
                "previously": {
                    "player": {
                        "match_stats": {"kills": 1},
                        "state": {"round_kills": 0},
                    }
                }
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["kill"])
        kill_event = filtered["events"][0]
        self.assertEqual(kill_event["killer"]["name"], "GrowthHormones")
        self.assertEqual(kill_event["kill_count"], 1)

    def test_player_identity_swap_does_not_create_fake_local_kill(self):
        previous = make_snapshot(
            round_number=6,
            round_phase="over",
            win_team="CT",
            ct_score=2,
            t_score=4,
            player=make_player("Pines", "CT", 100, round_kills=1, match_kills=4),
            allplayers={},
        )
        current = make_snapshot(
            round_number=6,
            round_phase="freezetime",
            win_team=None,
            ct_score=2,
            t_score=4,
            player=make_player("GrowthHormones", "T", 100, round_kills=0, match_kills=7),
            allplayers={},
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=10,
            payload={
                "previously": {
                    "player": {
                        "name": "Pines",
                        "steamid": "steam-Pines",
                        "match_stats": {"kills": 4},
                        "state": {"round_kills": 1},
                    },
                    "round": {"win_team": "CT", "phase": "over"},
                }
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["round_result"])

    def test_observer_switch_from_dead_victim_does_not_emit_duplicate_kill(self):
        previous = make_snapshot(
            round_number=5,
            player=make_player("Ulric", "T", 0, round_kills=0, match_kills=2),
            allplayers={},
        )
        current = make_snapshot(
            round_number=5,
            player=make_player("Dashen", "CT", 21, round_kills=1, match_kills=6),
            allplayers={},
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=11,
            payload={
                "previously": {
                    "player": {
                        "name": "Ulric",
                        "steamid": "steam-Ulric",
                        "team": "T",
                        "match_stats": {"kills": 2},
                        "state": {"health": 0, "round_kills": 0},
                    }
                }
            },
        )

        self.assertEqual(filtered["events"], [])

    def test_bomb_planted_event_is_emitted_for_local_player_session(self):
        previous = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "T", 100, round_kills=0, match_kills=1),
            allplayers={},
        )
        previous["round"]["phase"] = "live"
        current = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "T", 100, round_kills=0, match_kills=1),
            allplayers={},
        )
        current["round"]["phase"] = "live"
        current["round"]["bomb"] = "planted"

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=8,
            payload={"added": {"round": {"bomb": True}}},
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["bomb_event"])
        self.assertEqual(filtered["events"][0]["state_after"], "planted")
        self.assertEqual(filtered["trigger_paths"], ["round.bomb"])

    def test_round_result_keeps_previous_winner_if_current_payload_drops_it(self):
        previous = make_snapshot(
            round_phase="over",
            win_team="T",
            ct_score=0,
            t_score=1,
            round_number=1,
            allplayers={
                "20": make_player("Bob", "T", 80, round_kills=1, match_kills=3),
            },
        )
        current = make_snapshot(
            round_phase="freezetime",
            win_team=None,
            ct_score=0,
            t_score=1,
            round_number=1,
            allplayers={
                "10": make_player("Alice", "CT", 100, round_kills=0, match_kills=0),
                "20": make_player("Bob", "T", 100, round_kills=0, match_kills=3),
            },
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=6,
            payload={"previously": {"round": {"win_team": "T"}}},
        )

        self.assertEqual(filtered["events"], [{"event_type": "round_result", "round_phase_after": "freezetime", "winner": "T", "winner_score": 1}])

    def test_round_result_prunes_unrelated_bomb_path_noise(self):
        previous = make_snapshot(
            round_phase="live",
            win_team=None,
            ct_score=0,
            t_score=1,
            round_number=13,
            allplayers={},
        )
        previous["round"]["bomb"] = "planted"
        current = make_snapshot(
            round_phase="freezetime",
            win_team="T",
            ct_score=0,
            t_score=2,
            round_number=14,
            allplayers={},
        )

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=9,
            payload={
                "added": {"round": {"win_team": True}},
                "previously": {
                    "round": {"bomb": "planted", "phase": "live"},
                    "map": {"team_t": {"score": 1}},
                },
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["round_result"])
        self.assertNotIn("round.bomb", filtered["trigger_paths"])


if __name__ == "__main__":
    unittest.main()
