import json
import time

import httpx
import pytest
from helpers import FakeData, movie, series
from pydantic import ValidationError

from jfcurator.config import Config
from jfcurator.rules import evaluate
from jfcurator.rules.simkl import CACHE_FILE, TOKEN_FILE, SimklError, SimklSource, device_login


def rule(simkl=True, **kw):
    raw = {
        "jellyfin": {"url": "http://x", "api_key": "k", "user": "u"},
        "collections": {"T": {"libraries": ["L"], "type": "simkl_list", **kw}},
    }
    if simkl:
        raw["simkl"] = {"client_id": "cid"}
    return Config.model_validate(raw).collections["T"]


def names(picks):
    return sorted(p.item["Name"] for p in picks)


def show_entry(simkl, status, **ids):
    return {"status": status, "show": {"title": "x", "ids": {"simkl": simkl, **ids}}}


def movie_entry(simkl, status, **ids):
    return {"status": status, "movie": {"title": "x", "ids": {"simkl": simkl, **ids}}}


class FakeSimkl:
    """Serves /sync endpoints from mutable state and records every request."""

    def __init__(self, watchlist, activities):
        self.watchlist = watchlist  # {"shows": [...], "movies": [...], "anime": [...]}
        self.activities = activities
        self.delta = None
        self.requests: list[httpx.Request] = []
        self.unauthorized = set()
        self.refreshes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        path = request.url.path
        if path == "/oauth2/token":
            self.refreshes += 1
            return httpx.Response(
                200,
                json={
                    "access_token": f"at{self.refreshes}",
                    "refresh_token": "rt",
                    "expires_in": 604800,
                },
            )
        if token in self.unauthorized:
            return httpx.Response(401, json={"error": "invalid_token"})
        if path == "/sync/activities":
            return httpx.Response(200, json=self.activities)
        if path == "/sync/all-items":
            if "date_from" in request.url.params:
                return httpx.Response(200, json=self.delta)
            return httpx.Response(200, json=self.watchlist)
        return httpx.Response(404)

    def paths(self):
        return [
            r.url.path
            + ("?delta" if "date_from" in r.url.params else "")
            + ("?ids" if r.url.params.get("extended") == "simkl_ids_only" else "")
            for r in self.requests
        ]


def make_source(tmp_path, fake, token=None):
    token = token or {
        "access_token": "at0",
        "refresh_token": "rt",
        "expires_at": time.time() + 7 * 86400,
    }
    (tmp_path / TOKEN_FILE).write_text(json.dumps(token))
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return SimklSource("cid", tmp_path, http=http)


def acts(all_, removed="2020-01-01T00:00:00Z"):
    return {"all": all_, "tv_shows": {"all": all_, "removed_from_list": removed}, "movies": {}}


def test_status_aliases_and_requires_simkl_section():
    assert [s.value for s in rule(status=["planning", "Paused", "current"]).status] == [
        "plantowatch",
        "hold",
        "watching",
    ]
    with pytest.raises(ValidationError, match="simkl"):
        rule(simkl=False, status="completed")


def test_matching_by_kind_and_status(tmp_path):
    shows = FakeData(series=[series("Show", Tvdb="10"), series("Clash", Tmdb="5")])
    films = FakeData(movies=[movie("Film", Imdb="tt1"), movie("Clash Movie", Tmdb="5")])
    fake = FakeSimkl(
        {
            "shows": [
                show_entry(1, "plantowatch", tvdb="10"),
                show_entry(2, "plantowatch", tmdb=5),  # TV tmdb 5, not movie tmdb 5
                show_entry(3, "plantowatch", tvdb="999"),  # not in library
            ],
            "movies": [
                movie_entry(4, "plantowatch", imdb="tt1"),
                movie_entry(5, "completed", tmdb=5),
            ],
            "anime": None,
        },
        acts("t1"),
    )
    source = make_source(tmp_path, fake)
    got = evaluate(rule(status="planning"), [shows, films], simkl=source)
    assert names(got) == ["Clash", "Film", "Show"]
    got = evaluate(rule(status="completed"), [shows, films], simkl=source)
    assert names(got) == ["Clash Movie"]
    # Both rules share one sync.
    assert fake.paths() == ["/sync/activities", "/sync/all-items"]


def test_cache_is_reused_then_updated_by_delta_and_removals(tmp_path):
    fake = FakeSimkl(
        {"shows": [show_entry(1, "watching", tvdb="1"), show_entry(2, "watching", tvdb="2")]},
        acts("t1"),
    )
    assert len(make_source(tmp_path, fake).entries()) == 2

    # Nothing changed: activities only.
    fake.requests.clear()
    assert len(make_source(tmp_path, fake).entries()) == 2
    assert fake.paths() == ["/sync/activities"]

    # A status change: delta only, no full download.
    fake.requests.clear()
    fake.activities = acts("t2")
    fake.delta = {"shows": [show_entry(2, "completed", tvdb="2")]}
    entries = make_source(tmp_path, fake).entries()
    assert {e.status for e in entries} == {"watching", "completed"}
    assert fake.paths() == ["/sync/activities", "/sync/all-items?delta"]
    assert fake.requests[1].url.params["date_from"] == "t1"

    # A removal: delta plus an id-only listing to diff against.
    fake.requests.clear()
    fake.activities = acts("t3", removed="t3")
    fake.delta = {}
    fake.watchlist = {"shows": [show_entry(2, "completed")]}
    entries = make_source(tmp_path, fake).entries()
    assert [e.status for e in entries] == ["completed"]
    assert fake.paths() == ["/sync/activities", "/sync/all-items?delta", "/sync/all-items?ids"]
    assert json.loads((tmp_path / CACHE_FILE).read_text())["activities"]["all"] == "t3"


def test_empty_id_listing_refetches_instead_of_wiping(tmp_path):
    fake = FakeSimkl({"shows": [show_entry(1, "watching", tvdb="1")]}, acts("t1"))
    make_source(tmp_path, fake).entries()
    fake.requests.clear()
    fake.activities = acts("t2", removed="t2")
    fake.delta = {}
    original = fake.handler

    def unparseable_ids(request):
        if request.url.params.get("extended") == "simkl_ids_only":
            return httpx.Response(200, json={})
        return original(request)

    fake.handler = unparseable_ids
    assert len(make_source(tmp_path, fake).entries()) == 1
    assert fake.paths()[-1] == "/sync/all-items"


def test_failed_fetch_keeps_old_snapshot(tmp_path):
    fake = FakeSimkl({"shows": [show_entry(1, "watching", tvdb="1")]}, acts("t1"))
    make_source(tmp_path, fake).entries()
    fake.activities = acts("t2")
    fake.delta = None
    original = fake.handler

    def failing(request):
        if "date_from" in request.url.params:
            return httpx.Response(500, text="boom")
        return original(request)

    fake.handler = failing
    with pytest.raises(SimklError, match="500"):
        make_source(tmp_path, fake).entries()
    assert json.loads((tmp_path / CACHE_FILE).read_text())["activities"]["all"] == "t1"


def test_token_refreshed_near_expiry_and_on_401(tmp_path):
    fake = FakeSimkl({}, acts("t1"))
    source = make_source(
        tmp_path,
        fake,
        {"access_token": "old", "refresh_token": "rt", "expires_at": time.time() + 60},
    )
    source.entries()
    assert fake.refreshes == 1
    assert json.loads((tmp_path / TOKEN_FILE).read_text())["access_token"] == "at1"

    # Revoked/expired early: one refresh, then the request is retried.
    fake.unauthorized.add("at1")
    (tmp_path / CACHE_FILE).unlink()
    make_source(tmp_path, fake, json.loads((tmp_path / TOKEN_FILE).read_text())).entries()
    assert fake.refreshes == 2


def test_401_uses_token_another_process_refreshed(tmp_path):
    fake = FakeSimkl({}, acts("t1"))
    source = make_source(
        tmp_path,
        fake,
        {"access_token": "stale", "refresh_token": "rt", "expires_at": time.time() + 7 * 86400},
    )
    source._access_token()
    fake.unauthorized.add("stale")
    # Meanwhile the daemon refreshed and saved a new token.
    (tmp_path / TOKEN_FILE).write_text(
        json.dumps(
            {"access_token": "fresh", "refresh_token": "rt", "expires_at": time.time() + 7 * 86400}
        )
    )
    source.entries()
    assert fake.refreshes == 0


def test_missing_token_is_a_clear_error(tmp_path):
    source = SimklSource(
        "cid",
        tmp_path,
        http=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(500))),
    )
    with pytest.raises(SimklError, match="simkl-auth"):
        source.entries()


def test_device_login_polls_until_approved(tmp_path):
    polls = iter(["authorization_pending", "slow_down", None])
    waits = []

    def handler(request):
        if request.url.path == "/oauth2/device":
            return httpx.Response(
                200,
                json={
                    "device_code": "dc",
                    "user_code": "ABCD-EFGH",
                    "verification_uri": "https://simkl.com/pin",
                    "verification_uri_complete": "https://simkl.com/pin?user_code=ABCD-EFGH",
                    "expires_in": 900,
                    "interval": 5,
                },
            )
        error = next(polls)
        if error:
            return httpx.Response(400, json={"error": error})
        return httpx.Response(
            200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 604800}
        )

    shown = []
    path = device_login(
        "cid",
        tmp_path,
        shown.append,
        http=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=waits.append,
    )
    assert shown[0]["user_code"] == "ABCD-EFGH"
    assert waits == [5, 5, 10]
    assert json.loads(path.read_text())["refresh_token"] == "rt"
