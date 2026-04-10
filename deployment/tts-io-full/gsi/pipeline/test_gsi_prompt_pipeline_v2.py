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
        self.assertEqual(kill_event["association"]["status"], "paired")
        self.assertEqual(kill_event["association"]["method"], "single_kill_delta_and_health_drop")
        self.assertEqual(kill_event["players"]["killer"]["name"], "Alice")
        self.assertEqual(kill_event["players"]["victim"]["name"], "Bob")
        self.assertNotIn("position", kill_event["players"]["killer"])
        self.assertNotIn("position", kill_event["players"]["victim"])
        self.assertEqual(
            kill_event["players"]["killer"],
            {
                "armor": 100,
                "entity_id": "10",
                "health": 100,
                "match_kills": 9,
                "name": "Alice",
                "round_kills": 1,
                "team": "CT",
            },
        )
        self.assertEqual(kill_event["killer_round_kills_after"], 1)
        self.assertEqual(kill_event["killer_match_kills_after"], 9)
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
            filtered["important_delta_paths"],
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
        self.assertEqual(grenade_event["association"]["status"], "owner_resolved")
        self.assertEqual(grenade_event["grenade"]["owner_player"]["name"], "Alice")
        self.assertEqual(grenade_event["grenade"]["type"], "frag")
        self.assertEqual(filtered["important_delta_paths"], ["grenades.*.owner", "grenades.*.position", "grenades.*.type"])

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
        self.assertEqual(cluster["association"]["status"], "ambiguous_multi_actor")
        self.assertEqual(cluster["total_kill_count"], 2)
        self.assertEqual(sorted(k["name"] for k in cluster["killers"]), ["Alice", "Carol"])
        self.assertEqual(sorted(v["name"] for v in cluster["victims"]), ["Bob", "Dave"])

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

        self.assertEqual(filtered["events"], [{"event_index": 1, "event_type": "round_result", "round_phase_after": "freezetime", "winner": "T", "winner_score": 1}])


if __name__ == "__main__":
    unittest.main()
