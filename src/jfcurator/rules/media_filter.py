"""Select episodes/movies by the audio and subtitle streams they carry."""

from __future__ import annotations

from jfcurator.config import MediaFilterRule, StreamCriteria
from jfcurator.rules.base import Item, LibraryData, Pick

UNDEFINED_LANGUAGE = "und"


def item_streams(item: Item) -> list[dict]:
    """All media streams across every version of an item.

    Multi-version items are treated as one: a file is "missing Japanese audio"
    only if no version has it.
    """
    streams: list[dict] = []
    for source in item.get("MediaSources") or []:
        streams.extend(source.get("MediaStreams") or [])
    if not streams:
        streams = list(item.get("MediaStreams") or [])
    return streams


# Spellings Jellyfin uses for an untagged track (12.x reports the literal "undefined").
_UNDEFINED_ALIASES = {"", "und", "undefined", "unknown"}


def stream_language(stream: dict) -> str:
    lang = (stream.get("Language") or "").strip().lower()
    return UNDEFINED_LANGUAGE if lang in _UNDEFINED_ALIASES else lang


def streams_match(streams: list[dict], criteria: StreamCriteria) -> bool:
    if not criteria.include_external:
        streams = [s for s in streams if not s.get("IsExternal")]
    langs = {stream_language(s) for s in streams}

    if criteria.present is not None and bool(streams) != criteria.present:
        return False
    if criteria.languages and not langs.intersection(criteria.languages):
        return False
    return not (criteria.missing_language and langs.intersection(criteria.missing_language))


def item_matches(item: Item, rule: MediaFilterRule) -> bool:
    streams = item_streams(item)
    for kind, criteria in (("Audio", rule.match.audio), ("Subtitle", rule.match.subtitles)):
        if criteria is None or criteria.is_empty():
            continue
        of_kind = [s for s in streams if s.get("Type") == kind]
        if not streams_match(of_kind, criteria):
            return False
    return True


def evaluate_media_filter(rule: MediaFilterRule, data: LibraryData) -> list[Pick]:
    items = data.episodes_with_media if rule.item_type == "episode" else data.movies_with_media
    return [
        Pick(item=i, last_played=(i.get("UserData") or {}).get("LastPlayedDate"))
        for i in items
        if item_matches(i, rule)
    ]
