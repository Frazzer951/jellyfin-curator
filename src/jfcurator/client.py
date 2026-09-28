"""Thin, typed wrapper over the handful of Jellyfin REST endpoints this tool needs.

Deliberately hand-written rather than generated: Jellyfin's OpenAPI spec exposes
~1500 operations and we touch six.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Fields we ask Jellyfin to hydrate. MediaSources is expensive, so it is opt-in
# per call rather than part of this default set.
BASE_FIELDS = "UserData,ParentId,ProviderIds,PremiereDate,DateCreated,Path"

JsonDict = dict[str, Any]


class JellyfinError(RuntimeError):
    pass


class JellyfinClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        *,
        timeout: float = 60.0,
        verify_ssl: bool = True,
        page_size: int = 500,
        concurrency: int = 4,
    ) -> None:
        self.url = url.rstrip("/")
        self.page_size = page_size
        self.concurrency = concurrency
        self._http = httpx.Client(
            base_url=self.url,
            timeout=timeout,
            verify=verify_ssl,
            headers={
                "Authorization": f'MediaBrowser Token="{api_key}", Client="jfcurator"',
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> JellyfinClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- plumbing

    def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = self._http.request(method, path, **kw)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            body = e.response.text[:500]
            raise JellyfinError(f"{method} {path} -> {e.response.status_code}: {body}") from e
        except httpx.HTTPError as e:
            raise JellyfinError(f"{method} {path} failed: {e}") from e
        if not resp.content or resp.status_code == 204:
            return None
        return resp.json()

    def get(self, path: str, params: JsonDict | None = None) -> Any:
        return self._request("GET", path, params=params)

    def post(self, path: str, params: JsonDict | None = None, json: Any = None) -> Any:
        return self._request("POST", path, params=params, json=json)

    def delete(self, path: str, params: JsonDict | None = None) -> Any:
        return self._request("DELETE", path, params=params)

    # ------------------------------------------------------------------ system

    def system_info(self) -> JsonDict:
        return self.get("/System/Info")

    def users(self) -> list[JsonDict]:
        return self.get("/Users") or []

    def resolve_user(self, username: str) -> JsonDict:
        for user in self.users():
            if user.get("Name", "").lower() == username.lower():
                return user
        known = ", ".join(sorted(u.get("Name", "?") for u in self.users()))
        raise JellyfinError(f"no Jellyfin user named {username!r}; found: {known}")

    def libraries(self, user_id: str) -> list[JsonDict]:
        """Top-level library views (CollectionFolders) visible to the user."""
        data = self.get("/UserViews", {"userId": user_id})
        return (data or {}).get("Items", [])

    def resolve_library(self, user_id: str, name: str) -> JsonDict:
        for lib in self.libraries(user_id):
            if lib.get("Name", "").lower() == name.lower():
                return lib
        known = ", ".join(sorted(x.get("Name", "?") for x in self.libraries(user_id)))
        raise JellyfinError(f"no library named {name!r}; found: {known}")

    # ------------------------------------------------------------------- items

    def iter_items(
        self,
        *,
        user_id: str,
        parent_id: str | None = None,
        include_item_types: str | None = None,
        fields: str = BASE_FIELDS,
        extra: JsonDict | None = None,
    ) -> list[JsonDict]:
        """Page through /Items, accumulating results.

        Jellyfin caps page sizes in practice, and `MediaSources` responses are
        large, so everything goes through here rather than one giant request.
        """
        params: JsonDict = {
            "userId": user_id,
            "recursive": "true",
            "fields": fields,
            "enableTotalRecordCount": "true",
            "limit": self.page_size,
        }
        if parent_id:
            params["parentId"] = parent_id
        if include_item_types:
            params["includeItemTypes"] = include_item_types
        if extra:
            params.update(extra)

        first = self.get("/Items", {**params, "startIndex": 0}) or {}
        out: list[JsonDict] = list(first.get("Items", []))
        total = first.get("TotalRecordCount", len(out))
        if len(out) >= total or not out:
            return out

        # Stream-heavy fields cost the server ~15ms/item, so fetch the rest of
        # the pages concurrently. Results are reassembled in page order.
        starts = range(len(out), total, self.page_size)
        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            pages = pool.map(lambda st: self.get("/Items", {**params, "startIndex": st}), starts)
            for page in pages:
                out.extend((page or {}).get("Items", []))
        log.debug("fetched %d/%d items", len(out), total)
        return out

    # --------------------------------------------------------------- playlists

    def find_playlist(self, user_id: str, name: str) -> JsonDict | None:
        items = self.iter_items(
            user_id=user_id,
            include_item_types="Playlist",
            fields="",
        )
        for item in items:
            if item.get("Name") == name:
                return item
        return None

    def playlist_items(self, playlist_id: str, user_id: str) -> list[JsonDict]:
        """Ordered playlist entries. Each carries a `PlaylistItemId` used for
        removal and reordering, distinct from the underlying item `Id`."""
        data = self.get(f"/Playlists/{playlist_id}/Items", {"userId": user_id, "fields": ""})
        return (data or {}).get("Items", [])

    def create_playlist(self, name: str, user_id: str, item_ids: list[str]) -> str:
        body = {"Name": name, "Ids": item_ids, "UserId": user_id}
        data = self.post("/Playlists", json=body) or {}
        pid = data.get("Id")
        if not pid:
            raise JellyfinError(f"playlist create for {name!r} returned no Id: {data}")
        return pid

    def add_to_playlist(self, playlist_id: str, user_id: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        self.post(
            f"/Playlists/{playlist_id}/Items",
            {"ids": ",".join(item_ids), "userId": user_id},
        )

    def remove_from_playlist(self, playlist_id: str, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        self.delete(f"/Playlists/{playlist_id}/Items", {"entryIds": ",".join(entry_ids)})

    def move_playlist_item(self, playlist_id: str, entry_id: str, index: int) -> None:
        self.post(f"/Playlists/{playlist_id}/Items/{entry_id}/Move/{index}")

    # ------------------------------------------------------------- collections

    def find_collection(self, user_id: str, name: str) -> JsonDict | None:
        items = self.iter_items(user_id=user_id, include_item_types="BoxSet", fields="")
        for item in items:
            if item.get("Name") == name:
                return item
        return None

    def create_collection(self, name: str, item_ids: list[str]) -> str:
        data = self.post("/Collections", {"name": name, "ids": ",".join(item_ids)}) or {}
        cid = data.get("Id")
        if not cid:
            raise JellyfinError(f"collection create for {name!r} returned no Id: {data}")
        return cid

    def add_to_collection(self, collection_id: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        self.post(f"/Collections/{collection_id}/Items", {"ids": ",".join(item_ids)})

    def remove_from_collection(self, collection_id: str, item_ids: list[str]) -> None:
        if not item_ids:
            return
        self.delete(f"/Collections/{collection_id}/Items", {"ids": ",".join(item_ids)})

    # --------------------------------------------------------------------- misc

    def delete_item(self, item_id: str) -> None:
        """Used only to remove playlists/collections this tool owns."""
        self.delete(f"/Items/{item_id}")
