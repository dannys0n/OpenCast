from gsi_prompt_pipeline_v2 import as_dict
from tactical_rules_common_v4 import build_generic_summary, strip_empty
from tactical_rules_dust2_v4 import build_dust2_summary


def build_derived_tactical_summary(*, map_name, alive_players, current_events, previous_events, bomb_state, score):
    players = [as_dict(player) for player in alive_players or []]
    _ = current_events, previous_events
    summary = build_generic_summary(players, score, analysis_mode="generic")
    if map_name == "de_dust2":
        summary["analysis_mode"] = "map_specific"
        summary.update(build_dust2_summary(players, bomb_state))
    return strip_empty(summary)
