from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

# Logs and live progress share one console so Rich can interleave them cleanly.
console = Console(stderr=True)

# Loggers whose INFO/DEBUG output is per-request noise on the console.
_CHATTY = ("httpx", "httpcore")


class _QuietLibraries(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return record.levelno >= logging.WARNING or not record.name.startswith(_CHATTY)


def setup_logging(verbose: bool = False) -> None:
    """Console logging at INFO (DEBUG if verbose). The root logger itself runs at
    DEBUG so a per-run file (see `run_log_file`) captures everything."""
    handler = RichHandler(console=console, rich_tracebacks=True, show_path=verbose)
    handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not verbose:
        handler.addFilter(_QuietLibraries())
    logging.basicConfig(
        level=logging.DEBUG, format="%(message)s", datefmt="[%X]", handlers=[handler], force=True
    )
    # httpx logs each request at INFO, which is what we want in the file;
    # httpcore's connection-level DEBUG is too much even there.
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


@contextmanager
def run_log_file(path: Path | None) -> Iterator[None]:
    """Mirror all log records (DEBUG and up) into `path` for the duration."""
    if path is None:
        yield
        return
    handler = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logging.getLogger(__name__).error("could not open debug log %s: %s", path, e)
    if handler is None:
        yield
        return
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield
    finally:
        root.removeHandler(handler)
        handler.close()
