"""Command-line interface.

Most commands are deliberate stubs: they name the roadmap step that will give
them a body and exit NOT_IMPLEMENTED, rather than silently doing nothing. Only
the `db` subcommands are real at this point.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from shelfwarden import __version__
from shelfwarden.config import ConfigError, load_settings, require_plex
from shelfwarden.evals import export as export_module
from shelfwarden.library.base import LibraryError
from shelfwarden.library.plex import PlexLibrary, effective_request_params
from shelfwarden.models.item import FetchProfile
from shelfwarden.store import db as store_db


class ExitCode(IntEnum):
    """Process exit codes. CI branches on these, so they are part of the contract."""

    OK = 0
    ERROR = 1
    # 2 is Click's own usage-error code -- do not reuse it.
    FINDINGS = 3
    NOT_IMPLEMENTED = 4


class ExportProfile(StrEnum):
    """What `--profile` may be set to.

    Deliberately not `FetchProfile` itself. That enum has a third member, `STUB`,
    which marks a record that came from a *listing* -- it is something the export
    observes, never something an operator can ask the server for. Offering it in
    `--help` would advertise a choice that cannot work.

    CORE is the default: FULL differs by `checkFiles=1` alone, which maps to no
    field this model carries and costs a server-side stat per part. See
    docs/plans/step-0.4-export-census.md, Decision 2.
    """

    CORE = "core"
    FULL = "full"

    def fetch_profile(self) -> FetchProfile:
        return FetchProfile(self.value)


@dataclass(frozen=True)
class AppContext:
    db: Path
    verbose: bool


app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="An agentic media library steward for Plex.",
)
eval_app = typer.Typer(no_args_is_help=True, help="Run and score the eval suite.")
db_app = typer.Typer(no_args_is_help=True, help="Inspect and migrate the local store.")
app.add_typer(eval_app, name="eval")
app.add_typer(db_app, name="db")


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"shelfwarden {__version__}")
        raise typer.Exit(ExitCode.OK)


def _not_implemented(command: str, step: str, detail: str) -> NoReturn:
    typer.echo(f"{command} is not implemented yet — lands in roadmap step {step} ({detail}).")
    raise typer.Exit(ExitCode.NOT_IMPLEMENTED)


def _context(ctx: typer.Context) -> AppContext:
    return ctx.ensure_object(AppContext)


@app.callback()
def main(
    ctx: typer.Context,
    db: Annotated[
        Path, typer.Option("--db", help="Path to the local SQLite store.")
    ] = store_db.DEFAULT_DB,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose output.")] = False,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_version_callback,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """An agentic media library steward for Plex."""
    ctx.obj = AppContext(db=db, verbose=verbose)


# --------------------------------------------------------------------------
# Phase 0 / Phase 1 commands
# --------------------------------------------------------------------------


@app.command()
def export(
    ctx: typer.Context,
    count: Annotated[
        int,
        typer.Option("--count", help="Root items (movies, shows, authors) to export."),
    ] = export_module.DEFAULT_ROOTS,
    all_items: Annotated[
        bool, typer.Option("--all", help="Export every root item. Overrides --count.")
    ] = False,
    seed: Annotated[
        int, typer.Option("--seed", help="Seed for the slice selection.")
    ] = export_module.DEFAULT_SEED,
    max_records: Annotated[
        int,
        typer.Option(
            "--max-records",
            help="Record ceiling. A family that would exceed it is dropped whole, never truncated.",
        ),
    ] = export_module.DEFAULT_MAX_RECORDS,
    sections: Annotated[
        list[str] | None,
        typer.Option("--section", help="Restrict to these section ids. Repeatable."),
    ] = None,
    profile: Annotated[
        ExportProfile, typer.Option("--profile", help="How much to ask the server for.")
    ] = ExportProfile.CORE,
    out: Annotated[
        Path | None, typer.Option("--out", help="Export directory. Defaults to a timestamped one.")
    ] = None,
    census_only: Annotated[
        bool,
        typer.Option(
            "--census-only",
            help="Count the library without fetching items. Run this first to choose --count.",
        ),
    ] = False,
) -> None:
    """Export a slice of the Plex library as ground truth.

    Re-running this against an unchanged library produces byte-identical items.
    Note that "unchanged" is stronger than it sounds: Plex moves rating keys on
    rescan and bumps `updated_at` on a background metadata refresh, so a diff here
    is information about the server rather than a failure.
    """
    settings = load_settings()
    try:
        baseurl, token = require_plex(settings)
    except ConfigError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.ERROR) from exc

    now = datetime.now(UTC)
    directory = out or export_module.default_directory(settings.export_dir, now)
    fetch_profile = profile.fetch_profile()

    try:
        provider = PlexLibrary(baseurl, token)
        result = export_module.run_export(
            provider,
            directory,
            profile=fetch_profile,
            request_params=effective_request_params(fetch_profile),
            count=None if all_items else count,
            seed=seed,
            max_records=max_records,
            sections=tuple(sections or ()),
            census_only=census_only,
            secrets=settings.secrets,
            now=now,
        )
    except (export_module.ExportError, LibraryError) as exc:
        typer.echo(f"Export failed, nothing written: {exc}", err=True)
        raise typer.Exit(ExitCode.ERROR) from exc

    _report_export(result, census_only=census_only)


def _report_export(result: export_module.ExportResult, *, census_only: bool) -> None:
    manifest = result.manifest
    typer.echo(f"Wrote {result.directory}")
    for section in manifest.sections:
        typer.echo(
            f"  section {section.section_id} {section.title!r}: "
            f"{section.population} root item(s), "
            f"{section.exported_roots} exported ({section.exported_records} records)"
        )
    for skipped in manifest.skipped_sections:
        typer.echo(f"  skipped {skipped.section_id} {skipped.title!r}: {skipped.reason}")
    # Every drop is reported. A silently truncated export reads as full coverage.
    for dropped in manifest.dropped:
        typer.echo(f"  dropped {dropped.title!r} ({dropped.records} records): {dropped.reason}")

    if census_only:
        typer.echo("Census only — no items fetched. Read census.md, then choose --count.")
        return
    typer.echo(
        f"{manifest.counts.roots} root(s), {manifest.counts.records} record(s), "
        f"export id {manifest.export_id}"
    )
    typer.echo(f"Census: {result.directory / export_module.CENSUS_MARKDOWN_FILE}")


@app.command()
def scan(
    ctx: typer.Context,
    section: Annotated[str | None, typer.Option("--section", help="Section to scan.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Max items to scan.")] = None,
) -> None:
    """Diagnose problems in a library section. Read-only."""
    _not_implemented("scan", "1.6", "the agent loop")


@eval_app.command("run")
def eval_run(
    ctx: typer.Context,
    dataset: Annotated[str | None, typer.Option("--dataset", help="Dataset id.")] = None,
    provider: Annotated[str, typer.Option("--provider", help="Model provider.")] = "openai",
) -> None:
    """Run the agent against a labelled eval dataset."""
    _not_implemented("eval run", "1.7", "the eval runner")


@eval_app.command("score")
def eval_score(
    ctx: typer.Context,
    dataset: Annotated[str | None, typer.Option("--dataset", help="Dataset id.")] = None,
    proposals: Annotated[
        Path | None, typer.Option("--proposals", help="Proposed repair set to score.")
    ] = None,
) -> None:
    """Score a proposed repair set against a truth file."""
    _not_implemented("eval score", "0.8", "the scorer")


# --------------------------------------------------------------------------
# Phase 3 commands -- the repair stage
# --------------------------------------------------------------------------


@app.command()
def diff(
    ctx: typer.Context,
    plan_id: Annotated[str | None, typer.Argument(help="Plan to show.")] = None,
) -> None:
    """Show the proposed changes in a repair plan."""
    _not_implemented("diff", "3", "the repair stage")


@app.command()
def apply(
    ctx: typer.Context,
    plan_id: Annotated[str | None, typer.Argument(help="Plan to apply.")] = None,
    commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help="Actually apply the repairs. Without this flag the run is simulated.",
        ),
    ] = False,
) -> None:
    """Apply a repair plan. Simulates unless --commit is given."""
    # Spec §3.4: dry run is the default. Established in the CLI surface now, so
    # there is no window in which mutating code exists without this gate.
    typer.echo("Mode: COMMIT — repairs would be applied." if commit else "Mode: dry run.")
    _not_implemented("apply", "3", "the repair stage")


@app.command()
def revert(
    ctx: typer.Context,
    plan_id: Annotated[str, typer.Argument(help="Plan to revert.")],
) -> None:
    """Restore the library to its state before a plan was applied."""
    _not_implemented("revert", "3", "the repair stage")


# --------------------------------------------------------------------------
# Store management -- implemented
# --------------------------------------------------------------------------


@db_app.command("migrate")
def db_migrate(ctx: typer.Context) -> None:
    """Apply any pending migrations to the local store."""
    target = _context(ctx).db
    conn = store_db.connect(target)
    try:
        applied = store_db.migrate(conn)
    except store_db.MigrationTamperedError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(ExitCode.ERROR) from exc
    finally:
        conn.close()

    if applied:
        versions = ", ".join(f"{v:04d}" for v in applied)
        typer.echo(f"Applied {len(applied)} migration(s) to {target}: {versions}")
    else:
        typer.echo(f"{target} is up to date; nothing to apply.")


@db_app.command("status")
def db_status(ctx: typer.Context) -> None:
    """Show which migrations have been applied to the local store."""
    target = _context(ctx).db
    conn = store_db.connect(target)
    try:
        rows = store_db.schema_status(conn)
    finally:
        conn.close()

    if not rows:
        typer.echo(f"{target}: no migrations applied.")
        return

    typer.echo(f"{target}:")
    for row in rows:
        typer.echo(f"  {row.version:04d}  {row.name:<16}  {row.checksum[:12]}  {row.applied_at}")
