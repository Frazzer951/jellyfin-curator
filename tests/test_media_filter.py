import pytest
from helpers import FakeData, episode
from pydantic import ValidationError

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


def untagged_audio_eps():
    return [
        episode("A", 1, 1, audio=["jpn", "eng"]),
        episode("A", 1, 2, audio=["eng"]),
        episode("A", 1, 3, audio=[None]),
        episode("A", 1, 4, audio=["undefined", "eng"]),
    ]


def test_missing_japanese_audio_assumes_untagged_is_japanese_by_default():
    picks = evaluate(rule(audio={"missing_language": ["JPN"]}), FakeData(untagged_audio_eps()))
    assert names(picks) == ["A S01E02"]


def test_missing_japanese_audio_can_treat_untagged_as_missing():
    r = rule(audio={"missing_language": ["jpn"], "undefined_as_expected": False})
    picks = evaluate(r, FakeData(untagged_audio_eps()))
    assert names(picks) == ["A S01E02", "A S01E03", "A S01E04"]


def test_languages_counts_untagged_as_a_match_by_default():
    r = rule(audio={"languages": ["jpn"]})
    assert names(evaluate(r, FakeData(untagged_audio_eps()))) == [
        "A S01E01",
        "A S01E03",
        "A S01E04",
    ]


def test_criteria_are_anded():
    eps = [
        episode("A", 1, 1, audio=["eng"], subs=["eng"]),
        episode("A", 1, 2, audio=["eng"]),
    ]
    r = rule(audio={"missing_language": ["jpn"]}, subtitles={"present": False})
    assert names(evaluate(r, FakeData(eps))) == ["A S01E02"]


def overridden(overrides, **match) -> MediaFilterRule:
    return MediaFilterRule.model_validate(
        {"type": "media_filter", "name": "t", "match": match, "overrides": overrides}
    )


def test_series_override_checks_a_different_language():
    eps = [
        episode("Anime", 1, 1, audio=["jpn"]),
        episode("Anime", 1, 2, audio=["eng"]),
        episode("Donghua", 1, 1, audio=["chi"]),
        episode("Donghua", 1, 2, audio=["jpn"]),
    ]
    r = overridden(
        [{"series": ["donghua"], "match": {"audio": {"missing_language": ["chi", "zho"]}}}],
        audio={"missing_language": ["jpn"]},
    )
    assert names(evaluate(r, FakeData(eps))) == ["Anime S01E02", "Donghua S01E02"]


def test_series_override_inherits_unspecified_stream_type():
    eps = [
        episode("Donghua", 1, 1, audio=["eng"], subs=["eng"]),
        episode("Donghua", 1, 2, audio=["eng"]),
    ]
    r = overridden(
        [{"series": ["Donghua"], "match": {"audio": {"missing_language": ["chi"]}}}],
        audio={"missing_language": ["jpn"]},
        subtitles={"present": False},
    )
    assert names(evaluate(r, FakeData(eps))) == ["Donghua S01E02"]


def test_series_in_two_overrides_is_rejected():
    with pytest.raises(ValidationError, match="more than one override"):
        overridden(
            [
                {"series": ["A"], "match": {"audio": {"missing_language": ["chi"]}}},
                {"series": ["a "], "match": {"audio": {"missing_language": ["kor"]}}},
            ],
            audio={"missing_language": ["jpn"]},
        )


def test_unknown_override_series_warns(caplog):
    r = overridden(
        [{"series": ["Typo"], "match": {"audio": {"missing_language": ["chi"]}}}],
        audio={"missing_language": ["jpn"]},
    )
    evaluate(r, FakeData([episode("A", 1, 1, audio=["jpn"])]))
    assert "'Typo' not found" in caplog.text
