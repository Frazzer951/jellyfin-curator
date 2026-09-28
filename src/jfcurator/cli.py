"""Command-line entry point."""

from __future__ import annotations

import logging
import time
from collections import Counter
from pathlib import Path
from typing import Annotated

import typer
from dotenv import load_dotenv
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from jfcurator import __version__
from jfcurator.client import JellyfinError
from jfcurator.config import Config, load_config
from jfcurator.logging_setup import setup_logging
from jfcurator.rules.base import LibraryData
from jfcurator.rules.media_filter import item_streams, stream_language
from jfcurator.runner import make_client, run
from jfcurator.sync import SyncResult

# Local convenience: pick up ./.env. Real environment variables (e.g. Docker) win.
load_dotenv(override=False)

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()
log = logging.getLogger("jfcurator")

ConfigOpt = Annotated[
    Path,
    typer.Option("--config", "-c", envvar="JFCURATOR_CONFIG", help="Path to config.yaml."),
]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]


def _load(path: Path) -> Config:
    try:
        return load_config(path)
    except FileNotFoundError:
        console.print(f"[red]config not found:[/] {path}")
    except ValidationError as e:
        console.print(f"[red]invalid config[/] {path}:\n{e}")
    except ValueError as e:
        console.print(f"[red]config error:[/] {e}")
    raise typer.Exit(2)


def _print_results(results: list[SyncResult]) -> None:
    table = Table(title="Dry run" if results and results[0].dry_run else "Results")
    for col in ("Name", "Kind", "Action", "Items", "+", "-", "Moved"):
        table.add_column(col, justify="right" if col in "Items+-Moved" else "left")
    style = {"created": "green", "updated": "yellow", "deleted": "red"}
    for r in results:
        action = f"[{style.get(r.action, 'dim')}]{r.action}[/]"
        table.add_row(
            r.name, r.kind, action, str(r.total), str(r.added), str(r.removed), str(r.moved)
        )
    console.print(table)


@app.command()
def version() -> None:
    """Print version."""
    console.print(__version__)


@app.command()
def validate(config: ConfigOpt = Path("config/config.yaml")) -> None:
    """Validate the config file without contacting Jellyfin."""
    cfg = _load(config)
    rules = cfg.iter_rules()
    console.print(f"[green]ok[/] — {len(rules)} enabled rule(s)")
    for lib, rule in rules:
        console.print(f"  [{lib}] {rule.name}  ({rule.type} → {rule.target})")


@app.command()
def check(config: ConfigOpt = Path("config/config.yaml"), verbose: VerboseOpt = False) -> None:
    """Verify connectivity, the configured user, and library names."""
    setup_logging(verbose)
    cfg = _load(config)
    try:
        with make_client(cfg) as client:
            info = client.system_info()
            console.print(
                f"[green]connected[/] {info.get('ServerName')} (Jellyfin {info.get('Version')})"
            )
            user = client.resolve_user(cfg.jellyfin.user)
            console.print(f"user: {user['Name']} ({user['Id']})")
            libs = {lib["Name"]: lib for lib in client.libraries(user["Id"])}
            for name, lib in libs.items():
                used = "[cyan]configured[/]" if name in cfg.libraries else ""
                console.print(f"  library: {name} [dim]{lib.get('CollectionType', '')}[/] {used}")
            for name in cfg.libraries:
                if name not in libs:
                    console.print(f"[red]configured library not found:[/] {name}")
                    raise typer.Exit(1)
    except JellyfinError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e


@app.command()
def languages(
    library: Annotated[str, typer.Argument(help="Library name, e.g. Anime.")],
    config: ConfigOpt = Path("config/config.yaml"),
    verbose: VerboseOpt = False,
) -> None:
    """Tally audio/subtitle languages across a library's episodes.

    Useful before writing media_filter rules: shows how tracks are actually
    tagged (e.g. how many Japanese tracks are untagged `und`).
    """
    setup_logging(verbose)
    cfg = _load(config)
    with make_client(cfg) as client:
        user_id = client.resolve_user(cfg.jellyfin.user)["Id"]
        lib = client.resolve_library(user_id, library)
        data = LibraryData(client, user_id, lib["Id"])
        eps = data.episodes_with_media

    tallies = {"Audio": Counter(), "Subtitle": Counter()}
    none_of = Counter()
    for ep in eps:
        streams = item_streams(ep)
        for kind, counter in tallies.items():
            langs = {stream_language(s) for s in streams if s.get("Type") == kind}
            counter.update(langs)
            if not langs:
                none_of[kind] += 1

    console.print(f"{len(eps)} episodes in {library}")
    for kind, counter in tallies.items():
        table = Table(title=f"{kind} (episodes containing ≥1 track)")
        table.add_column("Language")
        table.add_column("Episodes", justify="right")
        for lang, n in counter.most_common():
            table.add_row(lang, str(n))
        table.add_row("[dim](none)[/]", str(none_of[kind]))
        console.print(table)


@app.command("run")
def run_cmd(
    config: ConfigOpt = Path("config/config.yaml"),
    dry_run: Annotated[bool, typer.Option("--dry-run", "-n", help="Show changes only.")] = False,
    only: Annotated[list[str] | None, typer.Option("--only", help="Run just this rule.")] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Evaluate all rules once and sync playlists/collections."""
    setup_logging(verbose)
    cfg = _load(config)
    try:
        results, errors = run(cfg, dry_run=dry_run, only=only or ())
    except JellyfinError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    _print_results(results)
    for err in errors:
        console.print(f"[red]error:[/] {err}")
    if errors:
        raise typer.Exit(1)


@app.command()
def daemon(
    config: ConfigOpt = Path("config/config.yaml"),
    interval: Annotated[
        int, typer.Option(envvar="JFCURATOR_INTERVAL", help="Seconds between runs.")
    ] = 3600,
    verbose: VerboseOpt = False,
) -> None:
    """Run forever, syncing every --interval seconds. Used by the Docker image.

    Config is re-read each cycle, so edits apply without a restart.
    """
    setup_logging(verbose)
    log.info("jfcurator %s daemon starting, interval=%ss", __version__, interval)
    while True:
        try:
            cfg = load_config(config)
            results, errors = run(cfg)
            _print_results(results)
            for err in errors:
                log.error(err)
        except (ValidationError, ValueError, OSError, JellyfinError) as e:
            log.error("run failed: %s", e)
        log.info("next run in %ss", interval)
        time.sleep(interval)
