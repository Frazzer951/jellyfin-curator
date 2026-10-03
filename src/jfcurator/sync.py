"""Reconcile a rule's desired item list with an existing playlist or collection.

Everything is diff-based: playlists are never deleted and recreated just to
change contents, so their Id (and anything clients key off it) stays stable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from jfcurator.client import JellyfinClient
from jfcurator.rules import Pick

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

    if not dry_run:
        client.remove_from_playlist(pid, [entries[i]["PlaylistItemId"] for i in plan.remove])
        for position, ids in plan.inserts:
            client.add_to_playlist(pid, user_id, ids, position=position)

    if result.added or result.removed or result.moved:
        result.action = "updated"
    return result


def collection_ids(picks: list[Pick]) -> list[str]:
    """Collections hold series/movies, not episodes: roll episodes up to their show."""
    ids = []
    for p in picks:
        if p.item.get("Type") == "Episode" and p.item.get("SeriesId"):
            ids.append(p.item["SeriesId"])
        else:
            ids.append(p.id)
    return _dedupe(ids)


def sync_collection(
    client: JellyfinClient,
    user_id: str,
    name: str,
    picks: list[Pick],
    *,
    delete_when_empty: bool,
    dry_run: bool,
) -> SyncResult:
    desired = collection_ids(picks)
    existing = client.find_collection(user_id, name)
    result = SyncResult(
        name=name, kind="collection", action="unchanged", total=len(desired), dry_run=dry_run
    )

    if existing is None:
        if not desired:
            result.action = "skipped"
            return result
        result.action, result.added = "created", len(desired)
        if not dry_run:
            client.create_collection(name, desired)
        return result

    cid = existing["Id"]
    if not desired and delete_when_empty:
        result.action = "deleted"
        if not dry_run:
            client.delete_item(cid)
        return result

    current = {
        i["Id"]
        for i in client.iter_items(
            user_id=user_id, parent_id=cid, fields="", extra={"recursive": "false"}
        )
    }
    to_add = [i for i in desired if i not in current]
    to_remove = [i for i in current if i not in set(desired)]
    result.added, result.removed = len(to_add), len(to_remove)
    if not dry_run:
        client.add_to_collection(cid, to_add)
        client.remove_from_collection(cid, to_remove)
    if to_add or to_remove:
        result.action = "updated"
    return result
