"""Reconcile a rule's desired item list with an existing playlist or collection.

Everything is diff-based: playlists are never deleted and recreated just to
change contents, so their Id (and anything clients key off it) stays stable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from jfcurator.client import JellyfinClient
from jfcurator.rules import Pick
from jfcurator.rules.base import Item

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    name: str
    kind: str
    action: str  # created | updated | unchanged | deleted | skipped
    total: int = 0
    added: int = 0
    removed: int = 0
    moved: int = 0
    dry_run: bool = False
    meta_changed: bool = False
    # Display names of what was added/removed, for the run summary.
    added_items: list[str] = field(default_factory=list)
    removed_items: list[str] = field(default_factory=list)


def item_label(item: Item) -> str:
    """Human-readable name: `Show S01E02 - Title` for episodes, `Title (Year)` otherwise."""
    name = item.get("Name") or item.get("Id", "?")
    if item.get("Type") == "Episode":
        season, number = item.get("ParentIndexNumber"), item.get("IndexNumber")
        code = f"S{season:02}E{number:02}" if season is not None and number is not None else ""
        return " ".join(filter(None, [item.get("SeriesName"), code, "-", name]))
    year = item.get("ProductionYear")
    return f"{name} ({year})" if year else name


def _longest_ordered(ranks: list[int]) -> set[int]:
    """Indices of a longest strictly increasing subsequence of `ranks` (-1 = never keep)."""
    tails: list[int] = []  # tails[k]: index ending the best run of length k+1
    prev: dict[int, int] = {}
    for i, r in enumerate(ranks):
        if r < 0:
            continue
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if ranks[tails[mid]] < r:
                lo = mid + 1
            else:
                hi = mid
        if lo:
            prev[i] = tails[lo - 1]
        if lo == len(tails):
            tails.append(i)
        else:
            tails[lo] = i
    keep: set[int] = set()
    i = tails[-1] if tails else None
    while i is not None:
        keep.add(i)
        i = prev.get(i)
    return keep


@dataclass
class PlaylistPlan:
    remove: list[int]  # indices into `current`
    inserts: list[tuple[int, list[str]]]  # (position, ids), applied in order after removal
    added: int
    removed: int
    moved: int


def plan_playlist(current: list[str], desired: list[str]) -> PlaylistPlan:
    """Edits that turn `current` into `desired` using only remove + insert-at-position.

    Jellyfin's Move endpoint needs a user session (it 400s under an API key), so
    reordering is done by pulling out-of-order entries and re-inserting them.
    The longest already-ordered run of entries is left untouched.
    """
    rank = {v: i for i, v in enumerate(desired)}
    keep = _longest_ordered([rank.get(v, -1) for v in current])
    remove = [i for i in range(len(current)) if i not in keep]
    kept = {current[i] for i in keep}

    inserts: list[tuple[int, list[str]]] = []
    for idx, want in enumerate(desired):
        if want in kept:
            continue
        if inserts and inserts[-1][0] + len(inserts[-1][1]) == idx:
            inserts[-1][1].append(want)
        else:
            inserts.append((idx, [want]))

    present = set(current)
    wanted_seen: set[str] = set()
    removed = 0
    for i in remove:
        v = current[i]
        if v not in rank or v in wanted_seen or v in kept:
            removed += 1
        wanted_seen.add(v)
    added = sum(1 for v in desired if v not in present)
    moved = len(remove) - removed
    return PlaylistPlan(remove, inserts, added, removed, moved)


def _dedupe(ids: list[str]) -> list[str]:
    return list(dict.fromkeys(ids))


def sync_playlist(
    client: JellyfinClient,
    user_id: str,
    name: str,
    picks: list[Pick],
    *,
    delete_when_empty: bool,
    dry_run: bool,
) -> SyncResult:
    desired = _dedupe([p.id for p in picks])
    existing = client.find_playlist(user_id, name)
    result = SyncResult(
        name=name, kind="playlist", action="unchanged", total=len(desired), dry_run=dry_run
    )

    if existing is None:
        if not desired:
            result.action = "skipped"
            return result
        result.action, result.added = "created", len(desired)
        result.added_items = _labels(picks, desired)
        if not dry_run:
            client.create_playlist(name, user_id, desired)
        return result

    pid = existing["Id"]
    if not desired and delete_when_empty:
        result.action = "deleted"
        if not dry_run:
            client.delete_item(pid)
        return result

    entries = client.playlist_items(pid, user_id)
    plan = plan_playlist([e["Id"] for e in entries], desired)
    result.added, result.removed, result.moved = plan.added, plan.removed, plan.moved
    present = {e["Id"] for e in entries}
    result.added_items = _labels(picks, [i for i in desired if i not in present])
    wanted = set(desired)
    result.removed_items = [item_label(e) for e in entries if e["Id"] not in wanted]

    if not dry_run:
        client.remove_from_playlist(pid, [entries[i]["PlaylistItemId"] for i in plan.remove])
        for position, ids in plan.inserts:
            client.add_to_playlist(pid, user_id, ids, position=position)

    if result.added or result.removed or result.moved:
        result.action = "updated"
    return result


def _labels(picks: list[Pick], ids: list[str], *, rollup: bool = False) -> list[str]:
    """Labels for `ids`, looked up among the picks (by rolled-up id for collections)."""
    by_id: dict[str, str] = {}
    for p in picks:
        if not rollup or p.rollup_id == p.id:
            by_id.setdefault(p.id, item_label(p.item))
        else:  # an episode standing in for its series
            by_id.setdefault(p.rollup_id, p.item.get("SeriesName") or p.rollup_id)
    return [by_id.get(i, i) for i in ids]


def collection_ids(picks: list[Pick]) -> list[str]:
    """Collections hold series/movies, not episodes: roll episodes up to their show."""
    return _dedupe([p.rollup_id for p in picks])


def _sync_metadata(
    client: JellyfinClient,
    user_id: str,
    collection_id: str | None,
    fields: dict[str, str],
    *,
    dry_run: bool,
) -> bool:
    """Set the given DTO fields on the collection. Returns whether anything changed."""
    if not fields:
        return False
    if collection_id is None:  # dry run of a collection we'd create
        return True
    item = client.get_item(user_id, collection_id)
    changed = {k: v for k, v in fields.items() if item.get(k) != v}
    if changed and not dry_run:
        client.update_item({**item, **changed})
    if changed:
        log.info("%s: setting %s", item.get("Name"), ", ".join(changed))
    return bool(changed)


def sync_collection(
    client: JellyfinClient,
    user_id: str,
    name: str,
    picks: list[Pick],
    *,
    delete_when_empty: bool,
    dry_run: bool,
    sort_title: str | None = None,
    summary: str | None = None,
) -> SyncResult:
    desired = collection_ids(picks)
    existing = client.find_collection(user_id, name)
    result = SyncResult(
        name=name, kind="collection", action="unchanged", total=len(desired), dry_run=dry_run
    )
    meta = {"ForcedSortName": sort_title, "Overview": summary}
    meta = {k: v for k, v in meta.items() if v is not None}

    if existing is None:
        if not desired:
            result.action = "skipped"
            return result
        result.action, result.added = "created", len(desired)
        result.added_items = _labels(picks, desired, rollup=True)
        cid = None if dry_run else client.create_collection(name, desired)
        result.meta_changed = _sync_metadata(client, user_id, cid, meta, dry_run=dry_run)
        return result

    cid = existing["Id"]
    if not desired and delete_when_empty:
        result.action = "deleted"
        if not dry_run:
            client.delete_item(cid)
        return result

    current = {
        i["Id"]: i
        for i in client.iter_items(
            user_id=user_id, parent_id=cid, fields="", extra={"recursive": "false"}
        )
    }
    to_add = [i for i in desired if i not in current]
    to_remove = [i for i in current if i not in set(desired)]
    result.added, result.removed = len(to_add), len(to_remove)
    result.added_items = _labels(picks, to_add, rollup=True)
    result.removed_items = [item_label(current[i]) for i in to_remove]
    if not dry_run:
        client.add_to_collection(cid, to_add)
        client.remove_from_collection(cid, to_remove)
    result.meta_changed = _sync_metadata(client, user_id, cid, meta, dry_run=dry_run)
    if to_add or to_remove or result.meta_changed:
        result.action = "updated"
    return result
