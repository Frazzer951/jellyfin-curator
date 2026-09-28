from __future__ import annotations

import itertools

_ids = itertools.count(1)


def episode(
    series: str,
    season: int,
    number: int,
    *,
    played: bool = False,
    position: int = 0,
    last_played: str | None = None,
    audio: list[str | None] | None = None,
    subs: list[str | None] | None = None,
    external_subs: list[str] | None = None,
) -> dict:
    streams = [{"Type": "Audio", "Language": lang} for lang in (audio or [])]
    streams += [{"Type": "Subtitle", "Language": lang} for lang in (subs or [])]
    streams += [
        {"Type": "Subtitle", "Language": lang, "IsExternal": True} for lang in (external_subs or [])
    ]
    return {
        "Id": f"ep{next(_ids)}",
        "Type": "Episode",
        "Name": f"{series} S{season:02}E{number:02}",
        "SeriesId": f"series-{series}",
        "SeriesName": series,
        "ParentIndexNumber": season,
        "IndexNumber": number,
        "UserData": {
            "Played": played,
            "PlaybackPositionTicks": position,
            "LastPlayedDate": last_played,
        },
        "MediaSources": [{"MediaStreams": streams}],
    }


class FakeData:
    """Stand-in for LibraryData with preloaded items."""

    def __init__(self, episodes: list[dict]):
        self.episodes = episodes
        self.episodes_with_media = episodes
        self.movies_with_media: list[dict] = []
