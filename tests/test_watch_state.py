from helpers import FakeData, episode

from jfcurator.config import ContinueWatchingRule, NewSeasonsRule
from jfcurator.rules import evaluate


def cw(**kw) -> ContinueWatchingRule:
    return ContinueWatchingRule.model_validate({"type": "continue_watching", "name": "t", **kw})


def ns(**kw) -> NewSeasonsRule:
    return NewSeasonsRule.model_validate({"type": "new_seasons", "name": "t", **kw})


def names(picks):
    return [p.item["Name"] for p in picks]


def test_continue_watching_picks_next_episode_sorted_by_recency():
    eps = [
        episode("Old", 1, 1, played=True, last_played="2026-01-01T00:00:00Z"),
        episode("Old", 1, 2),
        episode("New", 1, 1, played=True, last_played="2026-09-01T00:00:00Z"),
        episode("New", 1, 2, played=True, last_played="2026-09-02T00:00:00Z"),
        episode("New", 1, 3),
        episode("Done", 1, 1, played=True, last_played="2026-09-20T00:00:00Z"),
        episode("Untouched", 1, 1),
    ]
    assert names(evaluate(cw(), FakeData(eps))) == ["New S01E03", "Old S01E02"]


def test_continue_watching_resumes_after_furthest_not_first_gap():
    eps = [
        episode("A", 1, 1),  # skipped
        episode("A", 1, 2, played=True, last_played="2026-09-01T00:00:00Z"),
        episode("A", 1, 3),
    ]
    assert names(evaluate(cw(), FakeData(eps))) == ["A S01E03"]


def test_continue_watching_counts_partial_playback_as_started():
    eps = [episode("A", 1, 1, position=5_000), episode("A", 1, 2)]
    assert names(evaluate(cw(), FakeData(eps))) == ["A S01E01"]


def test_continue_watching_episodes_per_series():
    eps = [episode("A", 1, 1, played=True)] + [episode("A", 1, n) for n in (2, 3, 4)]
    assert names(evaluate(cw(episodes_per_series=2), FakeData(eps))) == ["A S01E02", "A S01E03"]


def test_new_seasons():
    eps = [
        # finished S1, S2 untouched -> pick S2E1
        episode("A", 1, 1, played=True, last_played="2026-05-01T00:00:00Z"),
        episode("A", 2, 1),
        episode("A", 2, 2),
        # finished S1, S2 in progress -> continue_watching's job, not ours
        episode("B", 1, 1, played=True),
        episode("B", 2, 1, played=True),
        episode("B", 2, 2),
        # never started -> not a "new season"
        episode("C", 1, 1),
        episode("C", 2, 1),
        # finished everything
        episode("D", 1, 1, played=True),
        # more recent than A
        episode("E", 1, 1, played=True, last_played="2026-08-01T00:00:00Z"),
        episode("E", 2, 1),
    ]
    assert names(evaluate(ns(), FakeData(eps))) == ["E S02E01", "A S02E01"]


def test_specials_ignored_by_default():
    eps = [
        episode("A", 0, 1, played=True),
        episode("A", 0, 2),
        episode("A", 1, 1, played=True),
    ]
    assert evaluate(cw(), FakeData(eps)) == []
    assert names(evaluate(cw(include_specials=True), FakeData(eps))) == ["A S00E02"]


def test_continue_watching_exclude_drops_whole_series():
    from jfcurator.config import Config

    cfg = Config.model_validate(
        {
            "jellyfin": {"url": "http://x", "api_key": "k", "user": "u"},
            "libraries": {"L": {"rules": {"CW": {"type": "continue_watching", "exclude": ["D"]}}}},
            "collections": {"D": {"type": "all_items", "libraries": ["L"]}},
        }
    )
    eps = [
        episode("Kept", 1, 1, played=True),
        episode("Kept", 1, 2),
        episode("Dropped", 1, 1, played=True),
        episode("Dropped", 1, 2),
    ]
    picks = evaluate(cfg.libraries["L"].rules["CW"], FakeData(eps), exclude={"series-Dropped"})
    assert [p.item["SeriesName"] for p in picks] == ["Kept"]
