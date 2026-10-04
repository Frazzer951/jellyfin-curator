import pytest
from helpers import FakeData, episode, movie, series
from pydantic import ValidationError

from jfcurator.config import Config
from jfcurator.rules import evaluate
from jfcurator.rules.anilist import Mapping


def rule(**kw):
    cfg = Config.model_validate(
        {
            "jellyfin": {"url": "http://x", "api_key": "k", "user": "u"},
            "collections": {"T": {"libraries": ["L"], **kw}},
        }
    )
    return cfg.collections["T"]


def names(picks):
    return sorted(p.item["Name"] for p in picks)


def test_binge_and_not_started_partition_shows():
    eps = [
        episode("Half", 1, 1, played=True),
        episode("Half", 1, 2),
        episode("Done", 1, 1, played=True),
        episode("Fresh", 1, 1),
        episode("Resumed", 1, 1, position=10),
        episode("Resumed", 1, 2),
        # A watched special alone doesn't count as starting the show.
        episode("SpecialOnly", 0, 1, played=True),
        episode("SpecialOnly", 1, 1),
    ]
    data = FakeData(eps)
    assert names(evaluate(rule(type="binge"), data)) == ["Half", "Resumed"]
    assert names(evaluate(rule(type="not_started"), data)) == ["Fresh", "SpecialOnly"]


def test_binge_series_status_filter():
    eps = [
        episode("A", 1, 1, played=True),
        episode("A", 1, 2),
        episode("B", 1, 1, played=True),
        episode("B", 1, 2),
    ]
    data = FakeData(eps, series=[series("A", status="Ended"), series("B", status="Continuing")])
    assert names(evaluate(rule(type="binge", series_status=["Ended"]), data)) == ["A"]


def test_all_items_with_exclude_and_multiple_libraries():
    shows = FakeData(series=[series("A"), series("B")])
    films = FakeData(movies=[movie("M")])
    picks = evaluate(rule(type="all_items"), [shows, films], exclude={"series-B"})
    assert names(picks) == ["A", "M"]


def test_exclude_applies_before_limit():
    data = FakeData(series=[series("A"), series("B"), series("C")])
    picks = evaluate(rule(type="all_items", limit=2), data, exclude={"series-A"})
    assert names(picks) == ["B", "C"]


class FakeAniList:
    def __init__(self, entries, mapping):
        self.entries = entries
        self._mapping = Mapping.from_json(mapping)

    def user_entries(self, username):
        return self.entries

    def top_rated(self, count):
        return [mid for mid, _ in self.entries][:count]

    def mapping(self):
        return self._mapping


def test_anilist_matching():
    anime = FakeData(
        series=[
            series("Direct", AniList="1"),
            series("ViaTvdb", Tvdb="500"),
            series("OnlyOva", Tvdb="600"),
        ]
    )
    films = FakeData(movies=[movie("Film", Imdb="tt9"), movie("ViaAnidb", AniDB="77")])
    mapping = {
        "10": {"anilist_id": 1},
        "20": {"anilist_id": 2, "tvdb_id": 500, "tvdb_season": 2},
        "30": {"anilist_id": 3, "tvdb_id": 600, "tvdb_season": 0},  # OVA: not the whole show
        "40": {"anilist_id": 4, "imdb_id": "tt8,tt9"},
        "77": {"anilist_id": 5},
    }
    source = FakeAniList(
        [
            (1, "planning"),
            (2, "planning"),
            (3, "planning"),
            (4, "planning"),
            (5, "planning"),
            (6, "planning"),
            (1, "completed"),
        ],
        mapping,
    )
    r = rule(type="anilist_list", username="me", status="planning")
    picks = evaluate(r, [anime, films], anilist=source)
    assert names(picks) == ["Direct", "Film", "ViaAnidb", "ViaTvdb"]

    r = rule(type="anilist_list", username="me", status=["Watching", "completed"])
    assert names(evaluate(r, [anime, films], anilist=source)) == ["Direct"]


def test_anilist_entries_mapping_to_same_show_dedupe():
    anime = FakeData(series=[series("Show", AniList="1", Tvdb="500")])
    mapping = {
        "1": {"anilist_id": 1, "tvdb_id": 500, "tvdb_season": 1},
        "2": {"anilist_id": 2, "tvdb_id": 500, "tvdb_season": 2},
    }
    source = FakeAniList([(1, "planning"), (2, "planning")], mapping)
    r = rule(type="anilist_top_rated", count=10)
    assert names(evaluate(r, anime, anilist=source)) == ["Show"]


def base(**extra):
    return {"jellyfin": {"url": "http://x", "api_key": "k", "user": "u"}, **extra}


def test_iter_rules_orders_dependencies_first():
    cfg = Config.model_validate(
        base(
            collections={
                "Rest": {"type": "all_items", "libraries": ["L"], "exclude": ["Plan"]},
                "Plan": {"type": "not_started", "libraries": ["L"]},
            }
        )
    )
    assert [r.name for r in cfg.iter_rules()] == ["Plan", "Rest"]


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (
            {"collections": {"A": {"type": "all_items", "libraries": ["L"], "exclude": ["Nope"]}}},
            "unknown rule",
        ),
        (
            {
                "collections": {
                    "A": {"type": "all_items", "libraries": ["L"], "exclude": ["B"]},
                    "B": {"type": "all_items", "libraries": ["L"], "exclude": ["A"]},
                }
            },
            "excludes itself",
        ),
        (
            {
                "collections": {
                    "A": {"type": "all_items", "libraries": ["L"], "exclude": ["B"]},
                    "B": {"type": "all_items", "libraries": ["L"], "enabled": False},
                }
            },
            "disabled",
        ),
        ({"collections": {"A": {"type": "all_items"}}}, "libraries"),
        (
            {
                "libraries": {"L": {"rules": {"A": {"type": "new_seasons"}}}},
                "collections": {"a": {"type": "all_items", "libraries": ["L"]}},
            },
            "more than once",
        ),
        (
            {"libraries": {"L": {"rules": {"A": {"type": "new_seasons", "libraries": ["M"]}}}}},
            "implied",
        ),
        (
            {"libraries": {"L": {"rules": {"A": {"type": "new_seasons", "sort_title": "x"}}}}},
            "only apply to collections",
        ),
        (
            {
                "collections": {
                    "A": {"type": "new_seasons", "libraries": ["L"], "target": "playlist"}
                }
            },
            "target must be collection",
        ),
    ],
)
def test_config_validation(extra, message):
    with pytest.raises(ValidationError, match=message):
        Config.model_validate(base(**extra))
