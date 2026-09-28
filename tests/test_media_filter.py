from helpers import FakeData, episode

from jfcurator.config import MediaFilterRule
from jfcurator.rules import evaluate


def rule(**match) -> MediaFilterRule:
    return MediaFilterRule.model_validate({"type": "media_filter", "name": "t", "match": match})


def names(picks):
    return [p.item["Name"] for p in picks]


def test_missing_subtitles_counts_external_by_default():
    eps = [
        episode("A", 1, 1, subs=["eng"]),
        episode("A", 1, 2),
        episode("A", 1, 3, external_subs=["eng"]),
    ]
    picks = evaluate(rule(subtitles={"present": False}), FakeData(eps))
    assert names(picks) == ["A S01E02"]


def test_missing_subtitles_embedded_only():
    eps = [episode("A", 1, 1, external_subs=["eng"])]
    r = rule(subtitles={"present": False, "include_external": False})
    assert names(evaluate(r, FakeData(eps))) == ["A S01E01"]


def test_missing_japanese_audio_treats_untagged_as_missing():
    eps = [
        episode("A", 1, 1, audio=["jpn", "eng"]),
        episode("A", 1, 2, audio=["eng"]),
        episode("A", 1, 3, audio=[None]),
        episode("A", 1, 4, audio=["undefined"]),
    ]
    picks = evaluate(rule(audio={"missing_language": ["JPN"]}), FakeData(eps))
    assert names(picks) == ["A S01E02", "A S01E03", "A S01E04"]


def test_criteria_are_anded():
    eps = [
        episode("A", 1, 1, audio=["eng"], subs=["eng"]),
        episode("A", 1, 2, audio=["eng"]),
    ]
    r = rule(audio={"missing_language": ["jpn"]}, subtitles={"present": False})
    assert names(evaluate(r, FakeData(eps))) == ["A S01E02"]
