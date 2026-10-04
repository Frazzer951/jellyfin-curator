"""Per-run Markdown summary of what each rule changed, written to a logs folder."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from jfcurator import __version__
from jfcurator.sync import SyncResult

log = logging.getLogger(__name__)

PREFIX = "run-"


def render_summary(
    results: list[SyncResult],
    errors: list[str],
    *,
    started: datetime,
    duration: float,
    dry_run: bool,
) -> str:
    changed = [r for r in results if r.action not in ("unchanged", "skipped")]
    quiet = [r for r in results if r not in changed]
    title = f"jfcurator run {started:%Y-%m-%d %H:%M:%S}" + (" (dry run)" if dry_run else "")
    out = [
        f"# {title}",
        "",
        f"jfcurator {__version__} · {duration:.1f}s · {len(results)} rule(s) synced, "
        f"{len(changed)} changed, {len(errors)} error(s)",
        "",
    ]
    if dry_run:
        out += ["Dry run: nothing below was actually applied.", ""]

    if results:
        out += ["| Name | Kind | Action | Items | + | - | Moved |", "|---|---|---|--:|--:|--:|--:|"]
        for r in results:
            action = r.action + (" (metadata)" if r.meta_changed else "")
            out.append(
                f"| {r.name} | {r.kind} | {action} | {r.total} | {r.added} | {r.removed} "
                f"| {r.moved} |"
            )
        out.append("")

    if errors:
        out += ["## Errors", "", *(f"- {e}" for e in errors), ""]

    for r in changed:
        out += [f"## {r.name} ({r.kind}, {r.action})", ""]
        if r.action == "deleted":
            out += ["Deleted: the rule selected nothing.", ""]
        if r.meta_changed:
            out += ["Sort title / summary updated.", ""]
        if r.moved:
            out += [f"Reordered {r.moved} item(s).", ""]
        for label, items in (("Added", r.added_items), ("Removed", r.removed_items)):
            if items:
                out += [f"**{label} ({len(items)})**", "", *(f"- {i}" for i in items), ""]

    if quiet:
        out += ["## No changes", "", ", ".join(f"{r.name} ({r.action})" for r in quiet), ""]
    return "\n".join(out)


def write_summary(
    directory: Path,
    results: list[SyncResult],
    errors: list[str],
    *,
    started: datetime,
    duration: float,
    dry_run: bool,
    keep: int,
) -> Path | None:
    """Write this run's summary and prune old runs' files down to `keep` (0 = keep all).

    Failures are logged, not raised: a full disk shouldn't fail the sync itself.
    """
    suffix = "-dryrun" if dry_run else ""
    path = directory / f"{PREFIX}{started:%Y%m%d-%H%M%S}{suffix}.md"
    try:
        directory.mkdir(parents=True, exist_ok=True)
        text = render_summary(results, errors, started=started, duration=duration, dry_run=dry_run)
        path.write_text(text)
        if keep:
            # Summaries and debug logs; timestamped names sort chronologically.
            for pattern in (f"{PREFIX}*.md", f"{PREFIX}*.log"):
                for old in sorted(directory.glob(pattern))[:-keep]:
                    old.unlink()
    except OSError as e:
        log.error("could not write run summary to %s: %s", directory, e)
        return None
    log.info("run summary: %s", path)
    return path
