"""Rule evaluators: pure functions from fetched library items to an ordered pick list."""

from __future__ import annotations

from jfcurator.config import ContinueWatchingRule, MediaFilterRule, NewSeasonsRule, Rule
from jfcurator.rules.base import LibraryData, Pick, sort_picks
from jfcurator.rules.media_filter import evaluate_media_filter
from jfcurator.rules.watch_state import evaluate_continue_watching, evaluate_new_seasons

__all__ = ["LibraryData", "Pick", "evaluate", "sort_picks"]


def evaluate(rule: Rule, data: LibraryData) -> list[Pick]:
    """Run a rule against a library and return its picks, sorted and limited."""
    match rule:
        case MediaFilterRule():
            picks = evaluate_media_filter(rule, data)
        case ContinueWatchingRule():
            picks = evaluate_continue_watching(rule, data)
        case NewSeasonsRule():
            picks = evaluate_new_seasons(rule, data)
        case _:
            raise TypeError(f"unhandled rule type: {type(rule).__name__}")

    picks = sort_picks(picks, rule.sort)
    if rule.limit is not None:
        picks = picks[: rule.limit]
    return picks
