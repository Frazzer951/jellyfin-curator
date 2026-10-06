"""Ties config, client, rules, and sync together for one run."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from contextlib import ExitStack, closing
from pathlib import Path

from jfcurator import __version__
from jfcurator.client import JellyfinClient, JellyfinError
from jfcurator.config import Config, Rule, SimklListRule
from jfcurator.rules import LibraryData, evaluate
from jfcurator.rules.anilist import AniListError, AniListSource
from jfcurator.rules.simkl import SimklError, SimklSource
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


def _with_dependencies(rules: list[Rule], selected: set[str]) -> set[str]:
    """`selected` plus every rule they exclude, transitively (lower-cased names)."""
    by_name = {r.name.lower(): r for r in rules}
    needed: set[str] = set()
    stack = list(selected & by_name.keys())
    while stack:
        name = stack.pop()
        if name not in needed:
            needed.add(name)
            stack.extend(ref.lower() for ref in by_name[name].exclude)
    return needed


def run(
    cfg: Config,
    *,
    dry_run: bool = False,
    only: Iterable[str] = (),
    state_dir: Path = Path("."),
) -> tuple[list[SyncResult], list[str]]:
    """Evaluate and sync every enabled rule. Returns (results, errors).

    `state_dir` holds files kept between runs (the Simkl token and cache).

    A failing rule is logged and skipped so one bad rule can't block the rest,
    though rules that exclude it are skipped too: without its picks they'd
    sweep in everything it was meant to keep out.
    """
    all_rules = cfg.iter_rules()
    only_set = {o.lower() for o in only}
    selected = only_set or {r.name.lower() for r in all_rules}
    # --only still evaluates (without syncing) whatever the chosen rules exclude.
    needed = _with_dependencies(all_rules, selected)
    rules = [r for r in all_rules if r.name.lower() in needed]
    results: list[SyncResult] = []
    errors: list[str] = []

    libraries = sorted({lib for r in rules for lib in r.libraries})
    log.info(
        "jfcurator %s%s: %d rule(s) across %d library(s): %s",
        __version__,
        " [DRY RUN, no changes will be made]" if dry_run else "",
        len(rules),
        len(libraries),
        ", ".join(libraries) or "-",
    )

    anilist = AniListSource()
    simkl = None
    if cfg.simkl and any(isinstance(r, SimklListRule) for r in rules):
        simkl = SimklSource(cfg.simkl.client_id, state_dir)
    with ExitStack() as stack:
        client = stack.enter_context(make_client(cfg))
        stack.enter_context(closing(anilist))
        if simkl:
            stack.enter_context(closing(simkl))
        log.info("connecting to %s", cfg.jellyfin.url)
        user = client.resolve_user(cfg.jellyfin.user)
        user_id = user["Id"]
        log.info("running as user %s", user["Name"])
        library_cache: dict[str, LibraryData] = {}
        # Each evaluated rule's picks as series/movie ids, for other rules' `exclude`.
        outputs: dict[str, set[str]] = {}

        def library(name: str) -> LibraryData:
            if name not in library_cache:
                lib = client.resolve_library(user_id, name)
                library_cache[name] = LibraryData(client, user_id, lib["Id"], name=lib["Name"])
            return library_cache[name]

        for n, rule in enumerate(rules, 1):
            full_name = f"{cfg.options.name_prefix}{rule.name}"
            where = ", ".join(rule.libraries)
            failed = [ref for ref in rule.exclude if ref.lower() not in outputs]
            if failed:
                msg = f"skipped: excluded rule(s) failed: {', '.join(failed)}"
                log.error("[%s] %s %s", where, full_name, msg)
                errors.append(f"{rule.name}: {msg}")
                continue
            try:
                log.info("(%d/%d) [%s] %s", n, len(rules), where, full_name)
                exclude = set().union(*(outputs[ref.lower()] for ref in rule.exclude))
                picks = evaluate(
                    rule,
                    [library(lib) for lib in rule.libraries],
                    anilist=anilist,
                    simkl=simkl,
                    exclude=exclude,
                )
                outputs[rule.name.lower()] = {p.rollup_id for p in picks}
                log.info("[%s] %s: %d item(s) selected", where, full_name, len(picks))
                if rule.name.lower() not in selected:
                    continue

                if rule.target == "playlist":
                    result = sync_playlist(
                        client,
                        user_id,
                        full_name,
                        picks,
                        delete_when_empty=rule.delete_when_empty,
                        dry_run=dry_run,
                    )
                else:
                    result = sync_collection(
                        client,
                        user_id,
                        full_name,
                        picks,
                        delete_when_empty=rule.delete_when_empty,
                        dry_run=dry_run,
                        sort_title=rule.sort_title,
                        summary=rule.summary,
                    )
                results.append(result)
            except (JellyfinError, AniListError, SimklError) as e:
                log.error("[%s] %s failed: %s", where, full_name, e)
                log.debug("traceback for %s", full_name, exc_info=True)
                errors.append(f"{rule.name}: {e}")

    for name in only_set - {r.name.lower() for r in all_rules}:
        errors.append(f"--only {name!r} matched no enabled rule")
    return results, errors
