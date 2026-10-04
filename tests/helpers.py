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


def series(name: str, *, status: str | None = None, **provider_ids: str) -> dict:
    return {
        "Id": f"series-{name}",
        "Type": "Series",
        "Name": name,
        "Status": status,
        "ProviderIds": provider_ids,
    }


def movie(name: str, **provider_ids: str) -> dict:
    return {"Id": f"movie-{name}", "Type": "Movie", "Name": name, "ProviderIds": provider_ids}


class FakeData:
    """Stand-in for LibraryData with preloaded items."""

    def __init__(
        self,
        episodes: list[dict] | None = None,
        *,
        series: list[dict] | None = None,
        movies: list[dict] | None = None,
    ):
        self.episodes = episodes or []
        self.episodes_with_media = self.episodes
        self.movies_with_media: list[dict] = movies or []
        self.movies = movies or []
        # Default: one Series item per distinct SeriesName in the episodes.
        if series is None:
            names = dict.fromkeys(e["SeriesName"] for e in self.episodes)
            series = [globals()["series"](n) for n in names]
        self.series = series
