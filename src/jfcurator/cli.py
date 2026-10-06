"""Command-line entry point."""

from __future__ import annotations

import logging
import time
from collections import Counter
from datetime import datetime
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
from jfcurator.logging_setup import run_log_file, setup_logging
from jfcurator.report import PREFIX, write_summary
from jfcurator.rules.base import LibraryData
from jfcurator.rules.media_filter import item_streams, stream_language
from jfcurator.rules.simkl import SimklError, device_login
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


def _run_once(
    cfg: Config, config_path: Path, *, dry_run: bool = False, only: list[str] | None = None
) -> tuple[list[SyncResult], list[str]]:
    """One sync, mirrored into a debug log and summarised in the logs folder.

    Never raises for run failures: they're logged (with traceback, in the
    debug log) and returned as errors so the summary still gets written.
    """
    log_dir = config_path.parent / cfg.options.log_dir if cfg.options.log_dir is not None else None
    started = datetime.now()
    stem = f"{PREFIX}{started:%Y%m%d-%H%M%S}" + ("-dryrun" if dry_run else "")
    results: list[SyncResult] = []
    errors: list[str] = []
    with run_log_file(log_dir / f"{stem}.log" if log_dir else None):
        try:
            results, errors = run(
                cfg, dry_run=dry_run, only=only or (), state_dir=config_path.parent
            )
        except JellyfinError as e:
            log.error("run failed: %s", e)
            log.debug("traceback", exc_info=True)
            errors.append(str(e))
        except Exception as e:
            log.exception("run crashed: %s", e)
            errors.append(f"unexpected {type(e).__name__}: {e}")
        if log_dir:
            write_summary(
                log_dir,
                results,
                errors,
                started=started,
                duration=(datetime.now() - started).total_seconds(),
                dry_run=dry_run,
                keep=cfg.options.keep_runs,
            )
    return results, errors


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
    for rule in rules:
        where = ", ".join(rule.libraries)
        console.print(f"  [{where}] {rule.name}  ({rule.type} → {rule.target})", markup=False)


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
            libs = {lib["Name"].lower(): lib for lib in client.libraries(user["Id"])}
            configured = {name.lower() for name in cfg.library_names()}
            for key, lib in libs.items():
                used = "[cyan]configured[/]" if key in configured else ""
                kind = lib.get("CollectionType", "")
                console.print(f"  library: {lib['Name']} [dim]{kind}[/] {used}")
            for name in cfg.library_names():
                if name.lower() not in libs:
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
        data = LibraryData(client, user_id, lib["Id"], name=lib["Name"])
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


@app.command("simkl-auth")
def simkl_auth(config: ConfigOpt = Path("config/config.yaml")) -> None:
    """Link your Simkl account (one-off; tokens are then refreshed automatically).

    Saves simkl-token.json next to the config file.
    """
    cfg = _load(config)
    if cfg.simkl is None:
        console.print("[red]add a `simkl:` section with your client_id to the config first[/]")
        raise typer.Exit(2)

    def show(grant: dict) -> None:
        console.print("Open this to connect your Simkl account:")
        console.print(f"  {grant['verification_uri_complete']}", markup=False)
        console.print(
            f"Or go to {grant['verification_uri']} and enter: [bold]{grant['user_code']}[/]"
        )
        console.print("[dim]waiting for approval…[/]")

    try:
        path = device_login(cfg.simkl.client_id, config.parent, show)
    except SimklError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1) from e
    console.print(f"[green]linked[/], token saved to {path}")


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
    results, errors = _run_once(cfg, config, dry_run=dry_run, only=only)
    if results:
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
        except (ValidationError, ValueError, OSError) as e:
            log.error("config error, skipping this cycle: %s", e)
        else:
            results, errors = _run_once(cfg, config)
            if results:
                _print_results(results)
            for err in errors:
                log.error(err)
        log.info("next run in %ss", interval)
        time.sleep(interval)
