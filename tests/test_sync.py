import itertools

from jfcurator.rules import Pick
from jfcurator.sync import plan_reorder, sync_playlist

_entry_ids = itertools.count()


def entry(item_id):
    # Real Jellyfin entry ids are unique even when the same item appears twice.
    return {"Id": item_id, "PlaylistItemId": f"e{next(_entry_ids)}"}


def apply(current, moves):
    work = current[:]
    for value, idx in moves:
        work.remove(value)
        work.insert(idx, value)
    return work


def test_plan_reorder_produces_desired_order():
    current = ["a", "b", "c", "d", "e"]
    desired = ["e", "c", "a", "d", "b"]
    moves = plan_reorder(current, desired)
    assert apply(current, moves) == desired


def test_plan_reorder_noop():
    assert plan_reorder(["a", "b"], ["a", "b"]) == []


class FakeClient:
    def __init__(self, playlist: list[str] | None):
        self.entries = None if playlist is None else [entry(i) for i in playlist]
        self.calls: list[tuple] = []

    def find_playlist(self, user_id, name):
        return None if self.entries is None else {"Id": "pl"}

    def playlist_items(self, pid, user_id):
        return list(self.entries)

    def create_playlist(self, name, user_id, ids):
        self.calls.append(("create", ids))
        self.entries = [entry(i) for i in ids]

    def remove_from_playlist(self, pid, entry_ids):
        self.calls.append(("remove", entry_ids))
        self.entries = [e for e in self.entries if e["PlaylistItemId"] not in entry_ids]

    def add_to_playlist(self, pid, user_id, ids):
        self.calls.append(("add", ids))
        self.entries += [entry(i) for i in ids]

    def move_playlist_item(self, pid, entry_id, idx):
        self.calls.append(("move", entry_id, idx))
        entry = next(e for e in self.entries if e["PlaylistItemId"] == entry_id)
        self.entries.remove(entry)
        self.entries.insert(idx, entry)

    def delete_item(self, item_id):
        self.calls.append(("delete", item_id))


def picks(*ids):
    return [Pick(item={"Id": i}) for i in ids]


def sync(client, *ids, dry_run=False):
    return sync_playlist(client, "u", "P", picks(*ids), delete_when_empty=True, dry_run=dry_run)


def test_creates_when_absent():
    c = FakeClient(None)
    r = sync(c, "a", "b")
    assert r.action == "created" and c.calls == [("create", ["a", "b"])]


def test_diff_update_ends_in_desired_order():
    c = FakeClient(["x", "b", "a", "b"])
    r = sync(c, "a", "b", "c")
    assert [e["Id"] for e in c.entries] == ["a", "b", "c"]
    assert (r.action, r.added, r.removed) == ("updated", 1, 2)


def test_dry_run_makes_no_calls_but_reports():
    c = FakeClient(["x", "b", "a"])
    r = sync(c, "a", "b", "c", dry_run=True)
    assert c.calls == []
    assert (r.added, r.removed, r.moved) == (1, 1, 1)


def test_unchanged():
    c = FakeClient(["a", "b"])
    assert sync(c, "a", "b").action == "unchanged"
    assert all(not call[1] for call in c.calls)  # only empty (no-op) calls


def test_empty_deletes():
    c = FakeClient(["a"])
    assert sync(c).action == "deleted"
    assert c.calls == [("delete", "pl")]
