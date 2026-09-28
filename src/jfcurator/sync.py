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


def plan_reorder(current: list[str], desired: list[str]) -> list[tuple[str, int]]:
    """Moves needed to turn `current` into `desired`, as (value, target_index).

    Both lists must hold the same elements. Moves are applied in sequence and
    mirror Jellyfin's "remove then insert at index" semantics.
    """
    work = current[:]
    moves: list[tuple[str, int]] = []
    for idx, want in enumerate(desired):
        if work[idx] == want:
            continue
        work.remove(want)
        work.insert(idx, want)
        moves.append((want, idx))
    return moves


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
    desired_set = set(desired)

    # Drop anything unwanted, plus duplicate entries of wanted items.
    seen: set[str] = set()
    stale: list[str] = []
    for e in entries:
        if e["Id"] not in desired_set or e["Id"] in seen:
            stale.append(e["PlaylistItemId"])
        else:
            seen.add(e["Id"])
    missing = [i for i in desired if i not in seen]
    result.removed, result.added = len(stale), len(missing)

    if dry_run:
        kept = [e["Id"] for e in entries if e["PlaylistItemId"] not in stale]
        result.moved = len(plan_reorder(kept + missing, desired))
    else:
        client.remove_from_playlist(pid, stale)
        client.add_to_playlist(pid, user_id, missing)
        if stale or missing:
            entries = client.playlist_items(pid, user_id)
        entry_for = {e["Id"]: e["PlaylistItemId"] for e in entries}
        moves = plan_reorder([e["Id"] for e in entries], desired)
        for item_id, idx in moves:
            client.move_playlist_item(pid, entry_for[item_id], idx)
        result.moved = len(moves)

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
