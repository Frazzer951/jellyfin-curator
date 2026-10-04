import itertools

from jfcurator.rules import Pick
from jfcurator.sync import plan_playlist, sync_playlist

_entry_ids = itertools.count()


def entry(item_id):
    # Real Jellyfin entry ids are unique even when the same item appears twice.
    return {"Id": item_id, "PlaylistItemId": f"e{next(_entry_ids)}"}


def apply(current, plan):
    work = [v for i, v in enumerate(current) if i not in plan.remove]
    for pos, ids in plan.inserts:
        work[pos:pos] = ids
    return work


def test_plan_produces_desired_order():
    current = ["a", "b", "c", "d", "e"]
    desired = ["e", "c", "a", "d", "b"]
    plan = plan_playlist(current, desired)
    assert apply(current, plan) == desired
    assert (plan.added, plan.removed, plan.moved) == (0, 0, 3)


def test_plan_noop():
    plan = plan_playlist(["a", "b"], ["a", "b"])
    assert (plan.remove, plan.inserts) == ([], [])


def test_plan_moving_one_item_touches_only_that_item():
    current = list("abcdefgh")
    desired = list("abcdefhg")
    plan = plan_playlist(current, desired)
    assert apply(current, plan) == desired
    assert len(plan.remove) == 1 and plan.moved == 1


def test_plan_handles_adds_removes_and_duplicates():
    current = ["x", "b", "a", "b", "d"]
    desired = ["a", "b", "c", "d"]
    plan = plan_playlist(current, desired)
    assert apply(current, plan) == desired
    assert (plan.added, plan.removed, plan.moved) == (1, 2, 0)


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

    def add_to_playlist(self, pid, user_id, ids, *, position=None):
        self.calls.append(("add", ids, position))
        pos = len(self.entries) if position is None else position
        self.entries[pos:pos] = [entry(i) for i in ids]

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


def test_reorder_uses_remove_and_insert():
    c = FakeClient(["a", "b", "c"])
    r = sync(c, "c", "a", "b")
    assert [e["Id"] for e in c.entries] == ["c", "a", "b"]
    assert (r.action, r.moved) == ("updated", 1)
    assert [call[0] for call in c.calls] == ["remove", "add"]


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


class FakeCollectionClient:
    def __init__(self, existing=None, members=(), item=None):
        self.existing = existing
        self.members = list(members)
        self.item = item or {}
        self.updates = []
        self.created = []

    def find_collection(self, user_id, name):
        return self.existing

    def create_collection(self, name, ids):
        self.created.append((name, ids))
        self.item = {"Id": "new", "Name": name}
        return "new"

    def iter_items(self, **kw):
        return [{"Id": i} for i in self.members]

    def add_to_collection(self, cid, ids):
        self.members += ids

    def remove_from_collection(self, cid, ids):
        self.members = [m for m in self.members if m not in ids]

    def get_item(self, user_id, item_id):
        return dict(self.item)

    def update_item(self, item):
        self.updates.append(item)
        self.item = item


def test_sync_collection_sets_metadata_only_when_changed():
    from jfcurator.sync import sync_collection

    picks = [Pick(item={"Id": "s1", "Type": "Series"})]
    client = FakeCollectionClient(
        existing={"Id": "c"}, members=["s1"], item={"Id": "c", "Name": "X", "Overview": "old"}
    )
    kw = dict(delete_when_empty=True, dry_run=False, sort_title="+1", summary="new")
    r = sync_collection(client, "u", "X", picks, **kw)
    assert r.action == "updated" and r.meta_changed
    assert client.updates == [{"Id": "c", "Name": "X", "Overview": "new", "ForcedSortName": "+1"}]

    r = sync_collection(client, "u", "X", picks, **kw)
    assert r.action == "unchanged" and len(client.updates) == 1


def test_sync_collection_create_then_metadata():
    from jfcurator.sync import sync_collection

    client = FakeCollectionClient()
    picks = [Pick(item={"Id": "e1", "Type": "Episode", "SeriesId": "s1"})]
    r = sync_collection(
        client, "u", "X", picks, delete_when_empty=True, dry_run=False, sort_title="+1"
    )
    assert r.action == "created" and client.created == [("X", ["s1"])]
    assert client.updates[0]["ForcedSortName"] == "+1"


def test_sync_collection_records_names():
    from jfcurator.sync import sync_collection

    class Client(FakeCollectionClient):
        def iter_items(self, **kw):
            return [{"Id": "old", "Name": "Old Show"}, {"Id": "s1", "Name": "Kept"}]

    picks = [
        Pick(item={"Id": "s1", "Type": "Series", "Name": "Kept"}),
        Pick(item={"Id": "e9", "Type": "Episode", "SeriesId": "s2", "SeriesName": "New Show"}),
    ]
    r = sync_collection(
        Client(existing={"Id": "c"}), "u", "X", picks, delete_when_empty=True, dry_run=True
    )
    assert r.added_items == ["New Show"] and r.removed_items == ["Old Show"]
