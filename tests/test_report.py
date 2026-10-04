import logging
from datetime import datetime

from jfcurator.client import ID_CHUNK, JellyfinClient
from jfcurator.logging_setup import run_log_file
from jfcurator.report import render_summary, write_summary
from jfcurator.sync import SyncResult, item_label


def test_item_label():
    ep = {
        "Type": "Episode",
        "SeriesName": "Show",
        "ParentIndexNumber": 1,
        "IndexNumber": 2,
        "Name": "Pilot",
    }
    assert item_label(ep) == "Show S01E02 - Pilot"
    assert item_label({"Type": "Movie", "Name": "Film", "ProductionYear": 2001}) == "Film (2001)"


def test_summary_lists_changes():
    results = [
        SyncResult(
            "Binge",
            "collection",
            "updated",
            total=3,
            added=1,
            removed=1,
            added_items=["New Show"],
            removed_items=["Old Show"],
        ),
        SyncResult("Quiet", "playlist", "unchanged", total=4),
    ]
    text = render_summary(
        results, ["Completed: boom"], started=datetime(2026, 10, 4), duration=1.0, dry_run=False
    )
    assert "**Added (1)**\n\n- New Show" in text
    assert "**Removed (1)**\n\n- Old Show" in text
    assert "- Completed: boom" in text
    assert "Quiet (unchanged)" in text


def test_write_summary_prunes_old_runs(tmp_path):
    for day in range(1, 5):
        (tmp_path / f"run-2026100{day}-000000.log").write_text("")
        write_summary(
            tmp_path, [], [], started=datetime(2026, 10, day), duration=0, dry_run=False, keep=2
        )
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "run-20261003-000000.log",
        "run-20261003-000000.md",
        "run-20261004-000000.log",
        "run-20261004-000000.md",
    ]


def test_run_log_file_captures_debug(tmp_path):
    path = tmp_path / "logs" / "run.log"
    logging.getLogger().setLevel(logging.DEBUG)
    with run_log_file(path):
        logging.getLogger("jfcurator.test").debug("hello debug")
    logging.getLogger("jfcurator.test").debug("after")
    text = path.read_text()
    assert "hello debug" in text and "after" not in text


def test_collection_ids_are_chunked():
    client = JellyfinClient("http://jf", "k")
    calls = []
    client._request = lambda method, path, **kw: calls.append((method, path, kw)) or {"Id": "c"}
    ids = [f"id{i}" for i in range(ID_CHUNK * 2 + 5)]
    assert client.create_collection("X", ids) == "c"
    sizes = [len(kw["params"]["ids"].split(",")) for _, _, kw in calls]
    assert sizes == [ID_CHUNK, ID_CHUNK, 5]
    assert [p for _, p, _ in calls] == [
        "/Collections",
        "/Collections/c/Items",
        "/Collections/c/Items",
    ]

    calls.clear()
    client.add_to_playlist("p", "u", ids, position=3)
    assert [kw["params"]["position"] for _, _, kw in calls] == [3, 3 + ID_CHUNK, 3 + 2 * ID_CHUNK]
