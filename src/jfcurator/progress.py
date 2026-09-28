"""Progress reporting: a live bar in a terminal, periodic log lines otherwise.

Docker/Unraid logs aren't a TTY, and a redrawn bar there turns into garbage,
so non-interactive runs fall back to one log line per 25%.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from jfcurator.logging_setup import console

log = logging.getLogger(__name__)

Reporter = Callable[[int, int], None]


@contextmanager
def track(label: str) -> Iterator[Reporter]:
    """Yield a `(done, total)` callback that renders progress for `label`."""
    if console.is_terminal:
        columns = (
            SpinnerColumn(),
            TextColumn("{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            TimeElapsedColumn(),
        )
        with Progress(*columns, console=console, transient=True) as bar:
            task = bar.add_task(label, total=None)
            yield lambda done, total: bar.update(task, completed=done, total=total)
        return

    next_pct = 25

    def report(done: int, total: int) -> None:
        nonlocal next_pct
        pct = done * 100 // total if total else 100
        if pct >= next_pct and done < total:
            log.info("%s: %d/%d (%d%%)", label, done, total, pct)
            next_pct = (pct // 25 + 1) * 25

    yield report
