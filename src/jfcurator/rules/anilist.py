"""AniList-driven collections: a user's lists and the site-wide top rated chart.

AniList ids are matched to library items directly when Jellyfin has an
`AniList` provider id (the AniList metadata plugin sets it on series), and
otherwise through the Kometa-Team Anime-IDs mapping, which links each AniList
entry to AniDB/TVDB/IMDb ids. Anime movies rely on the mapping: Jellyfin's
movie providers don't record AniList ids.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field

import httpx

from jfcurator.config import AniListListRule, AniListTopRatedRule
from jfcurator.rules.base import LibraryData, Pick, Ref, index_by_provider

log = logging.getLogger(__name__)

GRAPHQL_URL = "https://graphql.anilist.co"
MAPPING_URL = "https://raw.githubusercontent.com/Kometa-Team/Anime-IDs/master/anime_ids.json"
MAPPING_TTL = 24 * 3600
PAGE_SIZE = 50  # AniList's maximum perPage

_LIST_QUERY = """
query ($user: String, $chunk: Int) {
  MediaListCollection(userName: $user, type: ANIME, chunk: $chunk, perChunk: 500) {
    hasNextChunk
    lists { isCustomList entries { status media { id } } }
  }
}
"""

_TOP_RATED_QUERY = """
query ($page: Int, $perPage: Int) {
  Page(page: $page, perPage: $perPage) {
    pageInfo { hasNextPage }
    media(type: ANIME, sort: SCORE_DESC, averageScore_greater: 3) { id }
  }
}
"""


class AniListError(RuntimeError):
    pass


@dataclass
class Mapping:
    """AniList id -> provider ids from anime_ids.json, split by what they can match."""

    series: dict[int, set[Ref]] = field(default_factory=lambda: defaultdict(set))
    movies: dict[int, set[Ref]] = field(default_factory=lambda: defaultdict(set))

    @classmethod
    def from_json(cls, raw: dict[str, dict]) -> Mapping:
        m = cls()
        for anidb_id, entry in raw.items():
            anilist_id = entry.get("anilist_id")
            if not anilist_id:
                continue
            anidb: Ref = ("anidb", str(anidb_id))
            m.series[anilist_id].add(anidb)
            m.movies[anilist_id].add(anidb)
            # A TVDB id is the whole show. Season 0 entries are OVAs/specials
            # filed under some show: they shouldn't pull that show in.
            if entry.get("tvdb_id") and entry.get("tvdb_season") != 0:
                m.series[anilist_id].add(("tvdb", str(entry["tvdb_id"])))
            for imdb in filter(None, (entry.get("imdb_id") or "").split(",")):
                m.movies[anilist_id].add(("imdb", imdb.strip()))
        return m


# The mapping is ~2MB and changes slowly; keep it across daemon cycles.
_mapping_lock = threading.Lock()
_mapping_cache: tuple[float, Mapping] | None = None


class AniListSource:
    """AniList API access, cached for the lifetime of one run."""

    def __init__(self, http: httpx.Client | None = None) -> None:
        self._http = http or httpx.Client(timeout=30.0)
        self._lists: dict[str, list[tuple[int, str]]] = {}
        self._top: list[int] = []

    def close(self) -> None:
        self._http.close()

    def _query(self, query: str, variables: dict) -> dict:
        for attempt in range(4):
            try:
                resp = self._http.post(GRAPHQL_URL, json={"query": query, "variables": variables})
            except httpx.HTTPError as e:
                raise AniListError(f"AniList request failed: {e}") from e
            if resp.status_code == 429 and attempt < 3:
                wait = min(int(resp.headers.get("Retry-After", "60")), 65)
                log.warning("AniList rate limit hit, waiting %ss", wait)
                time.sleep(wait)
                continue
            body = resp.json() if resp.content else {}
            if resp.status_code != 200 or body.get("errors"):
                msgs = "; ".join(e.get("message", "?") for e in body.get("errors") or [])
                raise AniListError(f"AniList -> {resp.status_code}: {msgs or resp.text[:200]}")
            return body["data"]
        raise AniListError("AniList rate limit: gave up after retries")

    def user_entries(self, username: str) -> list[tuple[int, str]]:
        """(media id, status) for every entry on the user's standard lists."""
        key = username.lower()
        if key not in self._lists:
            entries: list[tuple[int, str]] = []
            chunk = 1
            while True:
                data = self._query(_LIST_QUERY, {"user": username, "chunk": chunk})
                coll = data["MediaListCollection"] or {}
                for lst in coll.get("lists") or []:
                    if lst.get("isCustomList"):
                        continue  # their entries also appear on a standard list
                    for e in lst.get("entries") or []:
                        entries.append((e["media"]["id"], e["status"].lower()))
                if not coll.get("hasNextChunk"):
                    break
                chunk += 1
            log.info("AniList: %d list entries for %s", len(entries), username)
            self._lists[key] = entries
        return self._lists[key]

    def top_rated(self, count: int) -> list[int]:
        page = len(self._top) // PAGE_SIZE + 1
        while len(self._top) < count:
            data = self._query(_TOP_RATED_QUERY, {"page": page, "perPage": PAGE_SIZE})
            self._top.extend(m["id"] for m in data["Page"]["media"])
            if not data["Page"]["pageInfo"]["hasNextPage"]:
                break
            page += 1
        return self._top[:count]

    def mapping(self) -> Mapping:
        global _mapping_cache
        with _mapping_lock:
            if _mapping_cache and time.monotonic() - _mapping_cache[0] < MAPPING_TTL:
                return _mapping_cache[1]
            try:
                resp = self._http.get(MAPPING_URL, follow_redirects=True)
                resp.raise_for_status()
                mapping = Mapping.from_json(resp.json())
            except (httpx.HTTPError, ValueError) as e:
                if _mapping_cache:
                    log.warning("anime id mapping refresh failed, using cached copy: %s", e)
                    return _mapping_cache[1]
                raise AniListError(f"could not fetch anime id mapping: {e}") from e
            _mapping_cache = (time.monotonic(), mapping)
            return mapping


def evaluate_anilist(
    rule: AniListListRule | AniListTopRatedRule,
    data: Sequence[LibraryData],
    source: AniListSource,
) -> list[Pick]:
    if isinstance(rule, AniListListRule):
        wanted = {s.value for s in rule.status}
        ids = [mid for mid, status in source.user_entries(rule.username) if status in wanted]
    else:
        ids = source.top_rated(rule.count)

    mapping = source.mapping()
    series = index_by_provider([i for d in data for i in d.series])
    movies = index_by_provider([i for d in data for i in d.movies])

    picks: dict[str, Pick] = {}
    unmatched = 0
    for anilist_id in ids:
        direct: Ref = ("anilist", str(anilist_id))
        found = [
            *(i for ref in {direct, *mapping.series.get(anilist_id, ())} for i in series[ref]),
            *(i for ref in {direct, *mapping.movies.get(anilist_id, ())} for i in movies[ref]),
        ]
        unmatched += not found
        for item in found:
            picks.setdefault(item["Id"], Pick(item=item))
    log.debug("%s: %d AniList entries, %d not in library", rule.name, len(ids), unmatched)
    return list(picks.values())
