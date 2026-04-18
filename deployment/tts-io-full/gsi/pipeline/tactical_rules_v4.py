import re

from gsi_prompt_pipeline_v2 import as_dict


LONG_BUCKET = {
    "Outside Long",
    "Long Doors",
    "Side Pit",
    "Pit",
    "Pit Plat",
    "Long",
    "Car",
    "Cross",
}
CAT_BUCKET = {
    "Catwalk",
    "Short",
    "Ramp",
    "Goose",
    "Default",
    "Plat",
    "Boost",
}
MID_BUCKET = {
    "Top Mid",
    "Mid",
    "Right Side Mid",
    "Suicide",
    "CT Mid",
    "Doors",
    "Blue",
}
B_BUCKET = {
    "Outside Tunnels",
    "Upper Tunnels",
    "Lower Tunnels",
    "Close",
    "B Car",
    "Fence",
    "Big Box",
    "Back Plat",
    "B Plat",
    "Back Site",
    "Double Stack",
    "B Default",
    "B Doors",
    "Window",
    "Scaffolding",
}
A_BUCKET = LONG_BUCKET | CAT_BUCKET


def strip_empty(value):
    if isinstance(value, dict):
        cleaned = {key: strip_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [strip_empty(item) for item in value]
        return [item for item in cleaned if item not in (None, "", [], {})]
    return value


def normalize_callout(callout):
    return re.sub(r"\s+", " ", str(callout or "").strip())


def count_known_positions(players, team=None):
    total = 0
    for player in players:
        player = as_dict(player)
        if team and player.get("team") != team:
            continue
        if normalize_callout(player.get("map_callout")):
            total += 1
    return total


def players_in_bucket(players, bucket, team=None):
    selected = []
    for player in players:
        player = as_dict(player)
        if team and player.get("team") != team:
            continue
        if normalize_callout(player.get("map_callout")) in bucket:
            selected.append(player)
    return selected


def control_state(players, bucket):
    ct_count = len(players_in_bucket(players, bucket, team="CT"))
    t_count = len(players_in_bucket(players, bucket, team="T"))
    if ct_count == 0 and t_count == 0:
        return "empty"
    if ct_count > 0 and t_count == 0:
        return "ct"
    if t_count > 0 and ct_count == 0:
        return "t"
    return "contested"


def pressure_level(bucket_t_count, known_t_positions):
    if known_t_positions <= 0:
        return "unknown"
    if bucket_t_count >= 2:
        return "high"
    if bucket_t_count == 1:
        return "medium"
    return "low"


def build_confidence(players):
    total_alive = len(players)
    known_positions = count_known_positions(players)
    if total_alive == 0 or known_positions == 0:
        return "low"
    if known_positions >= max(4, total_alive - 1):
        return "high"
    if known_positions >= max(2, total_alive // 2):
        return "medium"
    return "low"


def build_isolated_player(players):
    ct_a_players = players_in_bucket(players, A_BUCKET, team="CT")
    ct_b_players = players_in_bucket(players, B_BUCKET, team="CT")
    for site_players in (ct_b_players, ct_a_players):
        if len(site_players) == 1:
            player = site_players[0]
            if player.get("name") and player.get("map_callout"):
                return f"{player['name']} at {player['map_callout']}"
    return "none"


def build_rotation_favor(long_control, cat_control, mid_control, a_pressure, b_pressure, bomb_state):
    if bomb_state == "planted":
        return "t"
    if long_control == "ct" and cat_control == "ct":
        return "ct"
    if a_pressure == "high" and (long_control in {"t", "contested"} or cat_control in {"t", "contested"}):
        return "t"
    if b_pressure == "high" and mid_control != "ct":
        return "t"
    return "neutral"


def build_site_pressure(a_pressure, b_pressure, bomb_state):
    if bomb_state == "planted":
        return "post_plant"
    if a_pressure == "high" and b_pressure not in {"high", "medium"}:
        return "a_heavy"
    if b_pressure == "high" and a_pressure not in {"high", "medium"}:
        return "b_heavy"
    if a_pressure in {"medium", "high"} and b_pressure in {"low", "unknown"}:
        return "a_leaning"
    if b_pressure in {"medium", "high"} and a_pressure in {"low", "unknown"}:
        return "b_leaning"
    if a_pressure in {"medium", "high"} and b_pressure in {"medium", "high"}:
        return "split"
    return "unclear"


def build_next_move_hint(a_pressure, b_pressure, bomb_state):
    if bomb_state == "planted":
        return "post_plant_hold"
    if a_pressure == "high" and b_pressure not in {"high", "medium"}:
        return "a_commit"
    if b_pressure == "high" and a_pressure not in {"high", "medium"}:
        return "b_hit"
    if a_pressure in {"medium", "high"} and b_pressure in {"low", "unknown"}:
        return "a_leaning"
    if b_pressure in {"medium", "high"} and a_pressure in {"low", "unknown"}:
        return "b_leaning"
    if a_pressure in {"medium", "high"} and b_pressure in {"medium", "high"}:
        return "split"
    return "unclear"


def build_key_risk(*, site_pressure, long_control, cat_control, mid_control, isolated_player, rotation_favor, next_move_hint):
    if site_pressure in {"a_leaning", "a_heavy"} and long_control == "ct" and cat_control == "ct":
        return "a_split_timing_incomplete"
    if site_pressure in {"b_leaning", "b_heavy"} and mid_control == "ct":
        return "b_hit_readable"
    if isolated_player != "none" and "Back Site" in isolated_player:
        return "isolated_anchor"
    if rotation_favor == "ct" and next_move_hint in {"a_commit", "b_hit"}:
        return "defense_set_for_trades"
    return "none"


def build_dust2_summary(players, bomb_state):
    known_t_positions = count_known_positions(players, team="T")
    long_control = control_state(players, LONG_BUCKET)
    cat_control = control_state(players, CAT_BUCKET)
    mid_control = control_state(players, MID_BUCKET)
    a_pressure = pressure_level(len(players_in_bucket(players, A_BUCKET, team="T")), known_t_positions)
    b_pressure = pressure_level(len(players_in_bucket(players, B_BUCKET, team="T")), known_t_positions)
    site_pressure = build_site_pressure(a_pressure, b_pressure, bomb_state)
    next_move_hint = build_next_move_hint(a_pressure, b_pressure, bomb_state)
    isolated_player = build_isolated_player(players)
    rotation_favor = build_rotation_favor(
        long_control,
        cat_control,
        mid_control,
        a_pressure,
        b_pressure,
        bomb_state,
    )
    return {
        "map_control": {
            "long": long_control,
            "cat": cat_control,
            "mid": mid_control,
        },
        "pressure": {
            "site": site_pressure,
            "b": b_pressure,
        },
        "rotation_favor": rotation_favor,
        "isolated_player": isolated_player,
        "next_move_hint": next_move_hint,
        "key_risk": build_key_risk(
            site_pressure=site_pressure,
            long_control=long_control,
            cat_control=cat_control,
            mid_control=mid_control,
            isolated_player=isolated_player,
            rotation_favor=rotation_favor,
            next_move_hint=next_move_hint,
        ),
    }


def build_derived_tactical_summary(*, map_name, alive_players, current_events, previous_events, bomb_state):
    players = [as_dict(player) for player in alive_players or []]
    _ = current_events, previous_events
    summary = {
        "confidence": build_confidence(players),
        "next_move_hint": "unclear",
        "key_risk": "none",
    }
    if map_name == "de_dust2":
        summary.update(build_dust2_summary(players, bomb_state))
    return strip_empty(summary)
