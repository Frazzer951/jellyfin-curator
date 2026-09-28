from __future__ import annotations

import logging

from rich.logging import RichHandler


def setup_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        datefmt="[%X]",
        handlers=[RichHandler(rich_tracebacks=True, show_path=verbose)],
        force=True,
    )
    # httpx logs every request at INFO; that's noise unless debugging.
    logging.getLogger("httpx").setLevel(logging.DEBUG if verbose else logging.WARNING)
