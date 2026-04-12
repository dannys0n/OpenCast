import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("gsi_prompt_pipeline_v2.py")
SPEC = importlib.util.spec_from_file_location("gsi_prompt_pipeline_v2", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_player(name, team, health, round_kills=0, match_kills=0, match_deaths=0, match_assists=0, position=None):
    player = {
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
            "deaths": match_deaths,
            "assists": match_assists,
            "score": 0,
        },
    }
    if position is not None:
        player["position"] = position
    return player


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
                "10": make_player(
                    "Alice",
                    "CT",
                    100,
                    round_kills=0,
                    match_kills=8,
                    match_deaths=2,
                    match_assists=1,
                    position="-720, -830, 140",
                ),
                "20": make_player("Bob", "T", 100, round_kills=0, match_kills=5, match_deaths=6, match_assists=0),
            },
        )
        current = make_snapshot(
            round_number=9,
            allplayers={
                "10": make_player(
                    "Alice",
                    "CT",
                    100,
                    round_kills=1,
                    match_kills=9,
                    match_deaths=2,
                    match_assists=1,
                    position="-720, -830, 140",
                ),
                "20": make_player("Bob", "T", 0, round_kills=0, match_kills=5, match_deaths=7, match_assists=0),
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
                "kda": {"assists": 1, "deaths": 2, "kills": 9},
                "name": "Alice",
                "map_callout": "T Spawn",
                "round_kills": 1,
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
        self.assertEqual(filtered["events"][0]["alive_counts_after"], {"CT": 1, "T": 0})
        self.assertNotIn("trigger_paths", filtered)

    def test_detects_live_grenade_entity_but_not_equipped_grenade(self):
        player_with_equipped_grenade = make_player(
            "Alice",
            "CT",
            100,
            round_kills=0,
            match_kills=8,
            position="670, 507, 42",
        )
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
        self.assertEqual(grenade_event["owner_player"]["map_callout"], "Long Doors")
        self.assertEqual(grenade_event["grenade_type"], "frag")
        self.assertNotIn("grenade", grenade_event)
        self.assertNotIn("trigger_paths", filtered)

    def test_detects_local_player_grenade_throw_from_inventory_drop(self):
        previous_player = make_player(
            "Alice",
            "CT",
            100,
            round_kills=0,
            match_kills=8,
            position="670, 507, 42",
        )
        previous_player["weapons"] = {
            "weapon_2": {
                "name": "weapon_hegrenade",
                "type": "Grenade",
                "state": "active",
                "ammo_reserve": 1,
            },
            "weapon_3": {
                "name": "weapon_m4a1",
                "type": "Rifle",
                "state": "holstered",
            },
        }
        current_player = make_player(
            "Alice",
            "CT",
            100,
            round_kills=0,
            match_kills=8,
            position="670, 507, 42",
        )
        current_player["weapons"] = {
            "weapon_3": {
                "name": "weapon_m4a1",
                "type": "Rifle",
                "state": "active",
            },
        }

        filtered = MODULE.filter_important_events(
            make_snapshot(round_number=9, player=previous_player, allplayers={}),
            make_snapshot(round_number=9, player=current_player, allplayers={}),
            payload_sequence=5,
            payload={
                "previously": {
                    "player": {
                        "weapons": {
                            "weapon_2": {
                                "name": "weapon_hegrenade",
                                "type": "Grenade",
                                "state": "active",
                            }
                        }
                    }
                }
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["grenade_thrown"])
        grenade_event = filtered["events"][0]
        self.assertEqual(grenade_event["owner_player"]["name"], "Alice")
        self.assertEqual(grenade_event["grenade_type"], "frag")
        self.assertNotIn("trigger_paths", filtered)

    def test_resolves_nearest_dust2_callouts_from_text_file(self):
        self.assertEqual(MODULE.resolve_map_callout("de_dust2", "-720, -830, 140"), "T Spawn")
        self.assertEqual(MODULE.resolve_map_callout("de_dust2", "-1545, 1939, 53"), "B Car")

    def test_does_not_treat_local_grenade_equip_change_as_throw(self):
        previous_player = make_player("Alice", "CT", 100, round_kills=0, match_kills=8)
        previous_player["weapons"] = {
            "weapon_2": {
                "name": "weapon_hegrenade",
                "type": "Grenade",
                "state": "holstered",
                "ammo_reserve": 1,
            },
            "weapon_3": {
                "name": "weapon_m4a1",
                "type": "Rifle",
                "state": "active",
            },
        }
        current_player = make_player("Alice", "CT", 100, round_kills=0, match_kills=8)
        current_player["weapons"] = {
            "weapon_2": {
                "name": "weapon_hegrenade",
                "type": "Grenade",
                "state": "active",
                "ammo_reserve": 1,
            },
            "weapon_3": {
                "name": "weapon_m4a1",
                "type": "Rifle",
                "state": "holstered",
            },
        }

        filtered = MODULE.filter_important_events(
            make_snapshot(round_number=9, player=previous_player, allplayers={}),
            make_snapshot(round_number=9, player=current_player, allplayers={}),
            payload_sequence=6,
            payload={
                "previously": {
                    "player": {
                        "weapons": {
                            "weapon_2": {"state": "holstered"},
                            "weapon_3": {"state": "active"},
                        }
                    }
                }
            },
        )

        self.assertEqual(filtered["events"], [])

    def test_detects_grenade_detonation_from_last_seen_position_when_entity_disappears(self):
        spectator = make_player("Alice", "CT", 100, round_kills=0, match_kills=8, position="270, 2360, -90")
        previous = make_snapshot(
            round_number=9,
            player=spectator,
            allplayers={
                "494": make_player("Niles", "T", 100, round_kills=0, match_kills=3, position="-500, 1540, -114"),
            },
            grenades={
                "241": {
                    "owner": "494",
                    "type": "frag",
                    "position": "-497.7, 1539.2, -113.8",
                    "lifetime": "6.624",
                    "velocity": "0.000, 0.000, 0.000",
                }
            },
        )
        current = make_snapshot(
            round_number=9,
            player=spectator,
            allplayers={
                "494": make_player("Niles", "T", 100, round_kills=0, match_kills=3, position="-500, 1540, -114"),
            },
            grenades={},
        )

        filtered = MODULE.filter_important_events(previous, current, payload_sequence=7)

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["grenade_detonated"])
        grenade_event = filtered["events"][0]
        self.assertEqual(grenade_event["grenade_type"], "frag")
        self.assertEqual(grenade_event["detonation_callout"], "Doors")
        self.assertEqual(grenade_event["owner_player"]["name"], "Niles")

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

    def test_local_player_only_session_emits_scored_kill_without_allplayers(self):
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

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["player_scored_kill"])
        kill_event = filtered["events"][0]
        self.assertEqual(
            kill_event["player"],
            {
                "kda": {"assists": 0, "deaths": 0, "kills": 2},
                "name": "GrowthHormones",
                "round_kills": 1,
                "team": "T",
            },
        )
        self.assertEqual(kill_event["kill_count"], 1)

    def test_local_player_death_is_emitted_without_allplayers(self):
        previous = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "CT", 25, round_kills=0, match_kills=1),
            allplayers={},
        )
        current = make_snapshot(
            round_number=13,
            player=make_player("GrowthHormones", "CT", 0, round_kills=0, match_kills=1),
            allplayers={},
        )
        current["player"]["match_stats"]["deaths"] = 3
        previous["player"]["match_stats"]["deaths"] = 2

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=8,
            payload={
                "previously": {
                    "player": {
                        "match_stats": {"deaths": 2},
                        "state": {"health": 25},
                    }
                }
            },
        )

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["player_death"])
        death_event = filtered["events"][0]
        self.assertEqual(
            death_event["player"],
            {
                "kda": {"assists": 0, "deaths": 3, "kills": 1},
                "name": "GrowthHormones",
                "round_kills": 0,
                "team": "CT",
            },
        )
        self.assertNotIn("trigger_paths", filtered)

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

        self.assertEqual(filtered["events"], [])

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
        self.assertNotIn("trigger_paths", filtered)

    def test_round_result_prunes_grenade_inventory_trigger_noise(self):
        previous = make_snapshot(
            round_phase="live",
            win_team=None,
            ct_score=4,
            t_score=7,
            round_number=11,
            player=make_player("GrowthHormones", "CT", 2, round_kills=0, match_kills=7),
            allplayers={},
        )
        previous["player"]["match_stats"]["deaths"] = 6
        previous["player"]["weapons"] = {
            "weapon_1": {
                "name": "weapon_famas",
                "type": "Rifle",
                "state": "active",
            },
            "weapon_2": {
                "name": "weapon_hegrenade",
                "type": "Grenade",
                "state": "holstered",
                "ammo_reserve": 1,
            },
        }
        current = make_snapshot(
            round_phase="freezetime",
            win_team="T",
            ct_score=4,
            t_score=8,
            round_number=12,
            player=make_player("GrowthHormones", "CT", 0, round_kills=0, match_kills=7),
            allplayers={},
        )
        current["player"]["match_stats"]["deaths"] = 7
        current["player"]["weapons"] = {
            "weapon_1": {
                "name": "weapon_famas",
                "type": "Rifle",
                "state": "active",
            },
        }

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=12,
            payload={
                "added": {"round": {"win_team": True}},
                "previously": {
                    "map": {"team_t": {"score": 7}},
                    "player": {
                        "match_stats": {"deaths": 6},
                        "weapons": {
                            "weapon_2": {
                                "name": "weapon_hegrenade",
                                "type": "Grenade",
                                "state": "holstered",
                                "ammo_reserve": 1,
                            }
                        },
                    },
                    "round": {"phase": "live"},
                },
            },
        )

        self.assertEqual(
            [event["event_type"] for event in filtered["events"]],
            ["player_death"],
        )
        self.assertNotIn("trigger_paths", filtered)

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

        self.assertEqual(filtered["events"], [])

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

        self.assertEqual(filtered["events"], [])
        self.assertNotIn("trigger_paths", filtered)

    def test_bomb_explosion_round_end_filters_out_victim_death_events(self):
        previous = make_snapshot(
            round_phase="live",
            win_team=None,
            ct_score=4,
            t_score=7,
            round_number=11,
            player=make_player("GrowthHormones", "CT", 18, round_kills=0, match_kills=7, match_deaths=6),
            allplayers={},
        )
        previous["round"]["bomb"] = "planted"
        current = make_snapshot(
            round_phase="freezetime",
            win_team="T",
            ct_score=4,
            t_score=8,
            round_number=12,
            player=make_player("GrowthHormones", "CT", 0, round_kills=0, match_kills=7, match_deaths=7),
            allplayers={},
        )
        current["round"]["bomb"] = "exploded"

        filtered = MODULE.filter_important_events(
            previous,
            current,
            payload_sequence=14,
            payload={
                "added": {"round": {"win_team": True}},
                "previously": {
                    "map": {"team_t": {"score": 7}},
                    "player": {
                        "match_stats": {"deaths": 6},
                        "state": {"health": 18},
                    },
                    "round": {"bomb": "planted", "phase": "live"},
                },
            },
        )

        self.assertEqual(
            [event["event_type"] for event in filtered["events"]],
            ["bomb_event"],
        )
        self.assertEqual(filtered["events"][0]["state_after"], "exploded")

    def test_emits_game_over_event_when_map_phase_enters_gameover(self):
        previous = make_snapshot(
            round_phase="freezetime",
            win_team="T",
            ct_score=3,
            t_score=8,
            round_number=11,
            allplayers={},
        )
        previous["map"]["phase"] = "live"
        current = make_snapshot(
            round_phase="freezetime",
            win_team="T",
            ct_score=3,
            t_score=8,
            round_number=11,
            allplayers={},
        )
        current["map"]["phase"] = "gameover"

        filtered = MODULE.filter_important_events(previous, current, payload_sequence=98)

        self.assertEqual([event["event_type"] for event in filtered["events"]], ["game_over"])
        self.assertEqual(
            filtered["events"][0],
            {
                "event_type": "game_over",
                "final_score": {"CT": 3, "T": 8},
                "map_phase_after": "gameover",
                "winner": "T",
            },
        )

    def test_standalone_team_counter_event_is_filtered_out(self):
        previous = make_snapshot(
            round_number=4,
            allplayers={
                "10": make_player("Alice", "CT", 100),
                "20": make_player("Bob", "T", 100),
                "21": make_player("Dave", "T", 100),
                "22": make_player("Eli", "T", 100),
                "23": make_player("Finn", "T", 100),
            },
        )
        current = make_snapshot(
            round_number=4,
            allplayers={
                "10": make_player("Alice", "CT", 100),
                "20": make_player("Bob", "T", 100),
                "21": make_player("Dave", "T", 100),
                "22": make_player("Eli", "T", 100),
            },
        )

        filtered = MODULE.filter_important_events(previous, current, payload_sequence=99)

        self.assertEqual(filtered["events"], [])


if __name__ == "__main__":
    unittest.main()
