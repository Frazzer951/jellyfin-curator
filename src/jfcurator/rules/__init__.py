"""Rule evaluators: pure functions from fetched library items to an ordered pick list."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from jfcurator.config import (
    AllItemsRule,
    AniListListRule,
    AniListTopRatedRule,
    BingeRule,
    ContinueWatchingRule,
    MediaFilterRule,
    NewSeasonsRule,
    NotStartedRule,
    Rule,
)
from jfcurator.rules.anilist import AniListSource, evaluate_anilist
from jfcurator.rules.base import LibraryData, Pick, sort_picks
from jfcurator.rules.media_filter import evaluate_media_filter
from jfcurator.rules.watch_state import (
    evaluate_binge,
    evaluate_continue_watching,
    evaluate_new_seasons,
    evaluate_not_started,
)

__all__ = ["LibraryData", "Pick", "evaluate", "sort_picks"]


def _evaluate_one(rule: Rule, data: LibraryData) -> list[Pick]:
    match rule:
        case MediaFilterRule():
            return evaluate_media_filter(rule, data)
        case ContinueWatchingRule():
            return evaluate_continue_watching(rule, data)
        case NewSeasonsRule():
            return evaluate_new_seasons(rule, data)
        case BingeRule():
            return evaluate_binge(rule, data)
        case NotStartedRule():
            return evaluate_not_started(rule, data)
        case AllItemsRule():
            return [Pick(item=i) for i in data.series + data.movies]
    raise TypeError(f"unhandled rule type: {type(rule).__name__}")


def evaluate(
    rule: Rule,
    data: LibraryData | Sequence[LibraryData],
    *,
    anilist: AniListSource | None = None,
    exclude: Collection[str] = (),
) -> list[Pick]:
    """Run a rule against its libraries and return its picks, sorted and limited.

    `exclude` holds series/movie ids to drop (see `Pick.rollup_id`), applied
    before the limit so excluded items don't use up slots.
    """
    datas = list(data) if isinstance(data, Sequence) else [data]
    if isinstance(rule, AniListListRule | AniListTopRatedRule):
        if anilist is None:
            raise ValueError(f"{rule.name}: AniList rules need an AniList source")
        picks = evaluate_anilist(rule, datas, anilist)
    else:
        picks = [p for d in datas for p in _evaluate_one(rule, d)]

    if exclude:
        picks = [p for p in picks if p.rollup_id not in exclude]
    picks = sort_picks(picks, rule.sort)
    if rule.limit is not None:
        picks = picks[: rule.limit]
    return picks
