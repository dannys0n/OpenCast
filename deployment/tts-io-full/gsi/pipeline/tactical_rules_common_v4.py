import re

from gsi_prompt_pipeline_v2 import as_dict


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


def count_alive_by_team(players):
    counts = {"ct": 0, "t": 0}
    for player in players:
        team = str(as_dict(player).get("team") or "").upper()
        if team == "CT":
            counts["ct"] += 1
        elif team == "T":
            counts["t"] += 1
    return counts


def build_position_data(players):
    total_alive = len(players)
    known_positions = count_known_positions(players)
    if total_alive == 0 or known_positions == 0:
        return "none"
    if known_positions >= max(1, total_alive - 1):
        return "full"
    return "partial"


def build_score_context(score):
    score = as_dict(score)
    ct_score = int(score.get("CT") or 0)
    t_score = int(score.get("T") or 0)
    if ct_score == t_score:
        leader = "tied"
    elif ct_score > t_score:
        leader = "ct"
    else:
        leader = "t"
    margin = "close" if abs(ct_score - t_score) <= 2 else "clear"
    return {
        "leader": leader,
        "margin": margin,
    }


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


def build_generic_summary(players, score, *, analysis_mode="generic"):
    return strip_empty(
        {
            "analysis_mode": analysis_mode,
            "position_data": build_position_data(players),
            "alive_counts": count_alive_by_team(players),
            "score_context": build_score_context(score),
            "confidence": build_confidence(players),
            "next_move_hint": "unclear",
            "key_risk": "none",
        }
    )
