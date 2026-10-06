"""Simkl-driven collections: the authorized user's watchlists.

Unlike AniList, Simkl lists are private, so this needs an app `client_id` and
a user token (AUTH V2: 7-day access token, 180-day sliding refresh token),
obtained once with `jfcurator simkl-auth` and kept in `simkl-token.json` next
to the config.

Simkl suspends clients that re-download whole watchlists on a timer, so the
lists are cached in `simkl-cache.json` and kept current the way Simkl asks:
check `/sync/activities` each run, and only when it moved fetch the items
changed since the last snapshot (plus an id-only listing to spot removals).

Items carry IMDb/TMDB/TVDB (and for anime AniDB/AniList) ids, which match
Jellyfin provider ids directly.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx

from jfcurator import __version__
from jfcurator.config import SimklListRule
from jfcurator.rules.base import LibraryData, Pick, Ref, index_by_provider

log = logging.getLogger(__name__)

API_URL = "https://api.simkl.com"
APP_NAME = "jellyfin-curator"
TOKEN_FILE = "simkl-token.json"
CACHE_FILE = "simkl-cache.json"
# Refresh an access token this long before it expires.
REFRESH_MARGIN = 24 * 3600
# Simkl id keys that correspond to Jellyfin provider ids (lower-cased).
MATCH_IDS = ("imdb", "tmdb", "tvdb", "anidb", "anilist")
DOMAINS = ("tv_shows", "anime", "movies")

_APP_PARAMS = {"app-name": APP_NAME, "app-version": __version__}
_HEADERS = {"User-Agent": f"{APP_NAME}/{__version__}"}


class SimklError(RuntimeError):
    pass


def _write_private(path: Path, data: dict) -> None:
    """Atomically replace `path` with `data` as JSON, readable only by us."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.warning("ignoring unreadable %s: %s", path, e)
        return None


def _save_token(path: Path, body: dict) -> dict:
    token = {
        "access_token": body["access_token"],
        "refresh_token": body["refresh_token"],
        "expires_at": time.time() + int(body.get("expires_in") or 7 * 24 * 3600),
        "scope": body.get("scope"),
    }
    _write_private(path, token)
    return token


def _oauth_error(resp: httpx.Response) -> str:
    try:
        return resp.json().get("error") or resp.text[:200]
    except ValueError:
        return resp.text[:200]


def device_login(
    client_id: str,
    state_dir: Path,
    show: Callable[[dict], None],
    *,
    http: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Path:
    """Link a Simkl account with the device flow and store its tokens.

    `show` gets the device authorization response (`user_code`,
    `verification_uri`, `verification_uri_complete`) to put in front of the user.
    """
    http = http or httpx.Client(timeout=30.0, headers=_HEADERS)
    resp = http.post(
        f"{API_URL}/oauth2/device",
        params=_APP_PARAMS,
        data={"client_id": client_id, "scope": "media:read"},
    )
    if resp.status_code != 200:
        raise SimklError(f"device authorization failed: {_oauth_error(resp)}")
    grant = resp.json()
    show(grant)

    interval = int(grant.get("interval") or 5)
    deadline = time.monotonic() + int(grant.get("expires_in") or 900)
    while time.monotonic() < deadline:
        sleep(interval)
        resp = http.post(
            f"{API_URL}/oauth2/token",
            params=_APP_PARAMS,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": grant["device_code"],
            },
        )
        if resp.status_code == 200:
            path = state_dir / TOKEN_FILE
            _save_token(path, resp.json())
            return path
        match _oauth_error(resp):
            case "authorization_pending":
                continue
            case "slow_down":
                interval += 5
            case "expired_token":
                break
            case error:
                raise SimklError(f"authorization failed: {error}")
    raise SimklError("the code expired before it was approved; run simkl-auth again")


@dataclass(frozen=True)
class Entry:
    """One watchlist item: what it is, which list it's on, and its matchable ids."""

    kind: str  # "show" or "movie"
    status: str
    ids: frozenset[Ref]


def _parse_items(body: dict | None) -> dict[str, dict]:
    """/sync/all-items response -> {simkl id: cache record}."""
    items: dict[str, dict] = {}
    for key, entries in (body or {}).items():
        for e in entries or []:
            media = e.get("show") or e.get("movie") or {}
            ids = media.get("ids") or {}
            if not ids.get("simkl"):
                continue
            is_movie = key == "movies" or (key == "anime" and e.get("anime_type") == "movie")
            items[str(ids["simkl"])] = {
                "kind": "movie" if is_movie else "show",
                "status": e.get("status"),
                "ids": {k: str(ids[k]) for k in MATCH_IDS if ids.get(k)},
            }
    return items


def _simkl_ids(body: dict | None) -> set[str]:
    out: set[str] = set()
    for entries in (body or {}).values():
        for e in entries or []:
            media = e.get("show") or e.get("movie") or {}
            if (media.get("ids") or {}).get("simkl"):
                out.add(str(media["ids"]["simkl"]))
    return out


class SimklSource:
    """The linked user's Simkl watchlists, synced once per run."""

    def __init__(self, client_id: str, state_dir: Path, http: httpx.Client | None = None) -> None:
        self._client_id = client_id
        self._token_path = state_dir / TOKEN_FILE
        self._cache_path = state_dir / CACHE_FILE
        self._http = http or httpx.Client(timeout=60.0, headers=_HEADERS)
        self._token: dict | None = None
        self._entries: list[Entry] | None = None

    def close(self) -> None:
        self._http.close()

    # --- auth -----------------------------------------------------------------

    def _load_token(self) -> dict:
        token = _read_json(self._token_path)
        if not token or not token.get("refresh_token"):
            raise SimklError(
                f"no Simkl account linked ({self._token_path}): run `jfcurator simkl-auth`"
            )
        return token

    def _refresh(self, token: dict) -> dict:
        log.info("refreshing Simkl access token")
        resp = self._http.post(
            f"{API_URL}/oauth2/token",
            params=_APP_PARAMS,
            data={
                "grant_type": "refresh_token",
                "client_id": self._client_id,
                "refresh_token": token["refresh_token"],
            },
        )
        if resp.status_code != 200:
            raise SimklError(
                f"Simkl token refresh failed ({_oauth_error(resp)}): run `jfcurator simkl-auth`"
            )
        return _save_token(self._token_path, resp.json())

    def _access_token(self) -> str:
        if self._token is None:
            self._token = self._load_token()
        if self._token.get("expires_at", 0) - time.time() < REFRESH_MARGIN:
            self._token = self._refresh(self._token)
        return self._token["access_token"]

    def _after_unauthorized(self, used: str) -> None:
        # Refreshing a grant kills its previous access token, so if another
        # process (say a manual run beside the daemon) already refreshed, use
        # its token rather than refreshing again and cutting it off.
        on_disk = self._load_token()
        if on_disk["access_token"] != used:
            self._token = on_disk
        else:
            self._token = self._refresh(on_disk)

    # --- API ------------------------------------------------------------------

    def _get(self, path: str, **params: str) -> dict | None:
        for attempt in range(4):
            token = self._access_token()
            try:
                resp = self._http.get(
                    f"{API_URL}{path}",
                    params={"client_id": self._client_id, **_APP_PARAMS, **params},
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as e:
                raise SimklError(f"Simkl request failed: {e}") from e
            if resp.status_code == 401 and attempt == 0:
                self._after_unauthorized(token)
                continue
            if resp.status_code == 429 and attempt < 3:
                wait = min(int(resp.headers.get("Retry-After", "60")), 120)
                log.warning("Simkl rate limit hit, waiting %ss", wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise SimklError(f"Simkl {path} -> {resp.status_code}: {resp.text[:200]}")
            return resp.json() if resp.content else None
        raise SimklError(f"Simkl {path}: gave up after retries")

    def _sync(self) -> dict[str, dict]:
        """Bring the on-disk watchlist cache up to date and return its items."""
        # Read activities before fetching, so changes made mid-fetch are
        # picked up next time rather than hidden behind a newer snapshot.
        activities = self._get("/sync/activities") or {}
        cache = _read_json(self._cache_path)
        if (
            not cache
            or cache.get("client_id") != self._client_id
            or not (cache.get("activities") or {}).get("all")
        ):
            log.info("Simkl: fetching full watchlist")
            items = _parse_items(self._get("/sync/all-items"))
        elif cache["activities"]["all"] == activities.get("all"):
            log.debug("Simkl: no changes since %s", activities.get("all"))
            return cache["items"]
        else:
            before = cache["activities"]
            items = cache["items"]
            delta = _parse_items(self._get("/sync/all-items", date_from=before["all"]))
            items.update(delta)
            log.info("Simkl: %d watchlist item(s) changed", len(delta))
            removed = any(
                (activities.get(d) or {}).get("removed_from_list")
                != (before.get(d) or {}).get("removed_from_list")
                for d in DOMAINS
            )
            if removed:
                keep = _simkl_ids(self._get("/sync/all-items", extended="simkl_ids_only"))
                if keep or not items:
                    gone = items.keys() - keep
                    for simkl_id in gone:
                        del items[simkl_id]
                    log.info("Simkl: %d item(s) removed from watchlists", len(gone))
                else:
                    # An empty id listing is more likely a response we couldn't
                    # parse than a cleared watchlist: refetch rather than wipe.
                    log.warning("Simkl: id-only listing came back empty, refetching in full")
                    items = _parse_items(self._get("/sync/all-items"))
        _write_private(
            self._cache_path,
            {"client_id": self._client_id, "activities": activities, "items": items},
        )
        return items

    def entries(self) -> list[Entry]:
        if self._entries is None:
            items = self._sync()
            self._entries = [
                Entry(
                    kind=rec["kind"],
                    status=rec["status"],
                    ids=frozenset(rec["ids"].items()),
                )
                for rec in items.values()
            ]
            log.info("Simkl: %d watchlist item(s)", len(self._entries))
        return self._entries


def evaluate_simkl(
    rule: SimklListRule, data: Sequence[LibraryData], source: SimklSource
) -> list[Pick]:
    wanted = {s.value for s in rule.status}
    # TMDB ids are only unique per kind, so shows match series and movies movies.
    pools = {
        "show": index_by_provider([i for d in data for i in d.series]),
        "movie": index_by_provider([i for d in data for i in d.movies]),
    }
    picks: dict[str, Pick] = {}
    listed = unmatched = 0
    for entry in source.entries():
        if entry.status not in wanted:
            continue
        listed += 1
        found = [item for ref in entry.ids for item in pools[entry.kind].get(ref, ())]
        unmatched += not found
        for item in found:
            picks.setdefault(item["Id"], Pick(item=item))
    log.debug("%s: %d Simkl entries, %d not in library", rule.name, listed, unmatched)
    return list(picks.values())
