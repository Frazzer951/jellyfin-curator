"""Rules driven by the configured user's watch progress."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from jfcurator.config import BingeRule, ContinueWatchingRule, NewSeasonsRule, NotStartedRule
from jfcurator.rules.base import Item, LibraryData, Pick


def _user_data(ep: Item) -> dict:
    return ep.get("UserData") or {}


def is_played(ep: Item) -> bool:
    return bool(_user_data(ep).get("Played"))


def is_started(ep: Item) -> bool:
    """Played, or partially played (resume point set)."""
    return is_played(ep) or (_user_data(ep).get("PlaybackPositionTicks") or 0) > 0


def last_played(ep: Item) -> str | None:
    return _user_data(ep).get("LastPlayedDate")


@dataclass
class Season:
    number: int
    episodes: list[Item] = field(default_factory=list)

    @property
    def played_count(self) -> int:
        return sum(is_played(e) for e in self.episodes)

    @property
    def is_finished(self) -> bool:
        return bool(self.episodes) and all(is_played(e) for e in self.episodes)

    @property
    def is_untouched(self) -> bool:
        return not any(is_started(e) for e in self.episodes)

    @property
    def is_in_progress(self) -> bool:
        return not self.is_finished and not self.is_untouched

    @property
    def last_played(self) -> str | None:
        # ISO-8601 UTC strings from Jellyfin sort lexically.
        return max(filter(None, (last_played(e) for e in self.episodes)), default=None)

    def next_episodes(self, count: int) -> list[Item]:
        """Unplayed episodes starting after the furthest-watched one.

        Resuming after the furthest point (not the first gap) avoids surfacing
        an episode the user deliberately skipped. If nothing follows it, fall
        back to the earliest unplayed so a finished-out-of-order season still
        yields something.
        """
        furthest = max(
            (i for i, e in enumerate(self.episodes) if is_played(e)),
            default=-1,
        )
        # Any partially-watched episode is unplayed, so it naturally leads this list.
        after = [e for e in self.episodes[furthest + 1 :] if not is_played(e)]
        pool = after or [e for e in self.episodes if not is_played(e)]
        return pool[:count]


def group_by_series(episodes: list[Item], include_specials: bool) -> dict[str, list[Season]]:
    """series id -> seasons (ascending), each with episodes in airing order."""
    tree: dict[str, dict[int, Season]] = defaultdict(dict)
    for ep in episodes:
        series_id = ep.get("SeriesId")
        season_no = ep.get("ParentIndexNumber")
        if series_id is None or season_no is None:
            continue
        if season_no == 0 and not include_specials:
            continue
        season = tree[series_id].setdefault(season_no, Season(season_no))
        season.episodes.append(ep)

    out: dict[str, list[Season]] = {}
    for series_id, seasons in tree.items():
        ordered = sorted(seasons.values(), key=lambda s: s.number)
        for s in ordered:
            s.episodes.sort(key=lambda e: (e.get("IndexNumber") or 0, e.get("SortName") or ""))
        out[series_id] = ordered
    return out


def _series_last_played(seasons: list[Season]) -> str | None:
    return max(filter(None, (s.last_played for s in seasons)), default=None)


def evaluate_continue_watching(rule: ContinueWatchingRule, data: LibraryData) -> list[Pick]:
    picks: list[Pick] = []
    for seasons in group_by_series(data.episodes, rule.include_specials).values():
        in_progress = [s for s in seasons if s.is_in_progress]
        if not in_progress:
            continue
        # If several seasons are half-watched, follow the one touched most recently.
        current = max(in_progress, key=lambda s: s.last_played or "")
        recency = _series_last_played(seasons)
        for ep in current.next_episodes(rule.episodes_per_series):
            picks.append(Pick(item=ep, last_played=recency))
    return picks


def evaluate_new_seasons(rule: NewSeasonsRule, data: LibraryData) -> list[Pick]:
    picks: list[Pick] = []
    for seasons in group_by_series(data.episodes, rule.include_specials).values():
        watched = [s for s in seasons if not s.is_untouched]
        if not watched:
            continue  # never started the show: not a "new season", just unwatched
        latest_touched = watched[-1]
        if not latest_touched.is_finished:
            continue  # mid-season: belongs to continue_watching instead
        upcoming = [s for s in seasons if s.number > latest_touched.number and s.is_untouched]
        if not upcoming:
            continue
        first_ep = upcoming[0].episodes[0]
        picks.append(Pick(item=first_ep, last_played=_series_last_played(seasons)))
    return picks


def _series_with_episodes(
    data: LibraryData, include_specials: bool
) -> list[tuple[Item, list[Season], list[Item]]]:
    """(series, seasons, episodes) for every series that has episodes on disk."""
    by_series = group_by_series(data.episodes, include_specials)
    out = []
    for series in data.series:
        seasons = by_series.get(series["Id"])
        if seasons:
            out.append((series, seasons, [e for s in seasons for e in s.episodes]))
    return out


def evaluate_binge(rule: BingeRule, data: LibraryData) -> list[Pick]:
    picks: list[Pick] = []
    for series, seasons, eps in _series_with_episodes(data, rule.include_specials):
        if not any(is_started(e) for e in eps) or all(is_played(e) for e in eps):
            continue
        status = (series.get("Status") or "").lower()
        if rule.series_status is not None and status not in rule.series_status:
            continue
        picks.append(Pick(item=series, last_played=_series_last_played(seasons)))
    return picks


def evaluate_not_started(rule: NotStartedRule, data: LibraryData) -> list[Pick]:
    return [
        Pick(item=series)
        for series, _, eps in _series_with_episodes(data, rule.include_specials)
        if not any(is_started(e) for e in eps)
    ]
