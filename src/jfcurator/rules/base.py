"""Shared data plumbing for rules: lazily-fetched library contents and sorting."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from functools import cached_property
from typing import TYPE_CHECKING, Any

from jfcurator.config import Sort, SortSpec
from jfcurator.progress import track

if TYPE_CHECKING:
    from jfcurator.client import JellyfinClient

Item = dict[str, Any]

log = logging.getLogger(__name__)


@dataclass
class Pick:
    """An item selected by a rule.

    `last_played` is the rule's notion of recency, which may differ from the
    item's own: for watch-state rules it's the most recent play anywhere in the
    series, since the picked episode itself is by definition unplayed.
    """

    item: Item
    last_played: str | None = None

    @property
    def id(self) -> str:
        return self.item["Id"]


@dataclass
class LibraryData:
    """Library contents fetched on first access and shared across rules.

    Several rules usually target the same library; this keeps us to one
    /Items sweep per (library, shape) per run.
    """

    client: JellyfinClient
    user_id: str
    library_id: str
    name: str = ""
    _extra: dict[str, list[Item]] = field(default_factory=dict)

    def _fetch(self, item_type: str, fields: str) -> list[Item]:
        from jfcurator.client import BASE_FIELDS

        what = f"{item_type.lower()}s" + (" with media streams" if fields else "")
        label = f"[{self.name}] fetching {what}"
        log.info("%s…%s", label, " (slow on large libraries)" if fields else "")
        started = time.perf_counter()
        with track(label) as progress:
            items = self.client.iter_items(
                user_id=self.user_id,
                parent_id=self.library_id,
                include_item_types=item_type,
                fields=f"{BASE_FIELDS},{fields}" if fields else BASE_FIELDS,
                extra={"isMissing": "false"},
                progress=progress,
            )
        log.info("%s: %d done in %.1fs", label, len(items), time.perf_counter() - started)
        # Belt and braces: virtual "missing episode" placeholders have no file.
        return [i for i in items if i.get("LocationType") != "Virtual"]

    @cached_property
    def episodes(self) -> list[Item]:
        return self._fetch("Episode", "")

    @cached_property
    def episodes_with_media(self) -> list[Item]:
        return self._fetch("Episode", "MediaSources")

    @cached_property
    def movies_with_media(self) -> list[Item]:
        return self._fetch("Movie", "MediaSources")


def _series_key(item: Item) -> tuple:
    return (
        (item.get("SeriesName") or item.get("Name") or "").lower(),
        item.get("ParentIndexNumber") or 0,
        item.get("IndexNumber") or 0,
    )


def sort_picks(picks: list[Pick], spec: SortSpec) -> list[Pick]:
    if spec.key is Sort.RANDOM:
        shuffled = picks[:]
        random.shuffle(shuffled)
        return shuffled

    # Items missing the sort field always go last regardless of direction.
    def keyed(value_of) -> list[Pick]:
        have = [p for p in picks if value_of(p) is not None]
        lack = [p for p in picks if value_of(p) is None]
        have.sort(key=value_of, reverse=spec.descending)
        lack.sort(key=lambda p: _series_key(p.item))
        return have + lack

    match spec.key:
        case Sort.NAME:
            return keyed(lambda p: (p.item.get("Name") or "").lower())
        case Sort.SERIES:
            return keyed(lambda p: _series_key(p.item))
        case Sort.LAST_PLAYED:
            return keyed(lambda p: p.last_played)
        case Sort.DATE_ADDED:
            return keyed(lambda p: p.item.get("DateCreated"))
        case Sort.PREMIERE_DATE:
            return keyed(lambda p: p.item.get("PremiereDate"))
    raise ValueError(f"unhandled sort key {spec.key}")
