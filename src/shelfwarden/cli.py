"""Command-line interface.

Most commands are deliberate stubs: they name the roadmap step that will give
them a body and exit NOT_IMPLEMENTED, rather than silently doing nothing. Only
the `db` subcommands are real at this point.
"""

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from shelfwarden import __version__
from shelfwarden.store import db as store_db


class ExitCode(IntEnum):
    """Process exit codes. CI branches on these, so they are part of the contract."""

    OK = 0
    ERROR = 1
    # 2 is Click's own usage-error code -- do not reuse it.
    FINDINGS = 3
    NOT_IMPLEMENTED = 4


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
    count: Annotated[int, typer.Option("--count", help="Items to export.")] = 200,
) -> None:
    """Export a slice of the Plex library as ground truth."""
    _not_implemented("export", "0.4", "library export + census")


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
