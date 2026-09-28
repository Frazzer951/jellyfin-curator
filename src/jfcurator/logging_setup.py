from __future__ import annotations

import logging

from rich.console import Console
from rich.logging import RichHandler

# Logs and live progress share one console so Rich can interleave them cleanly.
console = Console(stderr=True)


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(console=console, rich_tracebacks=True, show_path=verbose)],
        force=True,
    )
    # httpx logs every request at INFO; that's noise unless debugging.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
