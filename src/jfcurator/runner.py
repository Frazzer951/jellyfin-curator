"""Ties config, client, rules, and sync together for one run."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from jfcurator import __version__
from jfcurator.client import JellyfinClient, JellyfinError
from jfcurator.config import Config
from jfcurator.rules import LibraryData, evaluate
from jfcurator.sync import SyncResult, sync_collection, sync_playlist

log = logging.getLogger(__name__)


def make_client(cfg: Config) -> JellyfinClient:
    jf = cfg.jellyfin
    return JellyfinClient(
        jf.url,
        jf.api_key,
        timeout=jf.timeout,
        verify_ssl=jf.verify_ssl,
        page_size=cfg.options.page_size,
        concurrency=cfg.options.concurrency,
    )


def run(
    cfg: Config,
    *,
    dry_run: bool = False,
    only: Iterable[str] = (),
) -> tuple[list[SyncResult], list[str]]:
    """Evaluate and sync every enabled rule. Returns (results, errors).

    A failing rule is logged and skipped so one bad rule can't block the rest.
    """
    only_set = {o.lower() for o in only}
    results: list[SyncResult] = []
    errors: list[str] = []

    rules = [(lib, r) for lib, r in cfg.iter_rules() if not only_set or r.name.lower() in only_set]
    libraries = sorted({lib for lib, _ in rules})
    log.info(
        "jfcurator %s%s: %d rule(s) across %d library(s): %s",
        __version__,
        " [DRY RUN, no changes will be made]" if dry_run else "",
        len(rules),
        len(libraries),
        ", ".join(libraries) or "-",
    )

    with make_client(cfg) as client:
        log.info("connecting to %s", cfg.jellyfin.url)
        user = client.resolve_user(cfg.jellyfin.user)
        user_id = user["Id"]
        log.info("running as user %s", user["Name"])
        library_cache: dict[str, LibraryData] = {}

        for n, (lib_name, rule) in enumerate(rules, 1):
            full_name = f"{cfg.options.name_prefix}{rule.name}"
            try:
                if lib_name not in library_cache:
                    lib = client.resolve_library(user_id, lib_name)
                    library_cache[lib_name] = LibraryData(
                        client, user_id, lib["Id"], name=lib["Name"]
                    )
                log.info("(%d/%d) [%s] %s", n, len(rules), lib_name, full_name)
                picks = evaluate(rule, library_cache[lib_name])
                log.info("[%s] %s: %d item(s) selected", lib_name, full_name, len(picks))

                sync = sync_playlist if rule.target == "playlist" else sync_collection
                results.append(
                    sync(
                        client,
                        user_id,
                        full_name,
                        picks,
                        delete_when_empty=rule.delete_when_empty,
                        dry_run=dry_run,
                    )
                )
            except JellyfinError as e:
                log.error("[%s] %s failed: %s", lib_name, full_name, e)
                errors.append(f"{lib_name}/{rule.name}: {e}")

    if only_set:
        seen = {r.name.lower() for _, r in cfg.iter_rules()}
        for name in only_set - seen:
            errors.append(f"--only {name!r} matched no enabled rule")
    return results, errors
