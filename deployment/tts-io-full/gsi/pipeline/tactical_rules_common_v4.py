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


def build_generic_summary(players):
    return strip_empty(
        {
            "confidence": build_confidence(players),
            "next_move_hint": "unclear",
            "key_risk": "none",
        }
    )
