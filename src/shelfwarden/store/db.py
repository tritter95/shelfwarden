"""SQLite store: connections, migrations, and the content-addressed blob table.

Every connection in the codebase comes from `connect()`. A bare `sqlite3.connect`
elsewhere skips the pragmas below and is a bug.
"""

import hashlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

DEFAULT_DB = Path(".shelfwarden/store.db")

_MIGRATIONS_PACKAGE = "shelfwarden.store.migrations"
_BUSY_TIMEOUT_MS = 5000


class StoreError(Exception):
    """Base class for store failures."""


class MigrationTamperedError(StoreError):
    """An already-applied migration file no longer matches its recorded checksum.

    Migrations are immutable once applied. Add a new one rather than editing an
    old one; editing silently desynchronises every database that already ran it.
    """


class MigrationNameError(StoreError):
    """A migration file is not named `NNNN_name.sql`."""


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    sql: str
    checksum: str


@dataclass(frozen=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: str


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    """Open a connection with the pragmas this project requires.

    The ordering here is load-bearing. `autocommit=False` opens a transaction
    immediately, and `PRAGMA journal_mode=WAL` cannot run inside one -- it raises
    "cannot change into wal mode from within a transaction". So: connect in
    autocommit mode, set the pragmas, then switch to explicit transaction control.

    `journal_mode` persists in the database file; `foreign_keys` and
    `busy_timeout` are per-connection and must be set every time.
    """
    path = Path(path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(path, autocommit=True)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.row_factory = sqlite3.Row
    conn.autocommit = False
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Commit on clean exit, roll back on any exception."""
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    conn.commit()


@contextmanager
def _immediate(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block inside an explicit `BEGIN IMMEDIATE`.

    Takes the write lock up front so two processes migrating at once serialise
    rather than one failing partway. Requires autocommit mode, because under
    `autocommit=False` sqlite3 has already opened a transaction and `BEGIN`
    would fail.
    """
    previous = conn.autocommit
    conn.commit()
    conn.autocommit = True
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        conn.autocommit = previous
        raise
    conn.execute("COMMIT")
    conn.autocommit = previous


def _discover_migrations() -> list[Migration]:
    """Load `NNNN_name.sql` files from the migrations package, ordered by version.

    Uses `importlib.resources` rather than `__file__` so this keeps working when
    the package is installed non-editable.
    """
    found: list[Migration] = []
    for entry in resources.files(_MIGRATIONS_PACKAGE).iterdir():
        if not entry.name.endswith(".sql"):
            continue
        prefix, separator, remainder = entry.name.partition("_")
        if not separator or not prefix.isdigit():
            raise MigrationNameError(
                f"Migration {entry.name!r} must be named NNNN_name.sql "
                f"(e.g. 0001_blobs.sql); found {entry.name!r}."
            )
        sql = entry.read_text(encoding="utf-8")
        found.append(
            Migration(
                version=int(prefix),
                name=remainder.removesuffix(".sql"),
                sql=sql,
                checksum=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
            )
        )
    found.sort(key=lambda m: m.version)
    return found


def _ensure_schema_version(conn: sqlite3.Connection) -> None:
    """Create the runner's own bookkeeping table.

    This is deliberately not a migration -- a migration runner cannot use a table
    that a migration would have to create.
    """
    with transaction(conn):
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                checksum   TEXT NOT NULL,
                applied_at TEXT NOT NULL
            )
            """
        )


def schema_status(conn: sqlite3.Connection) -> list[AppliedMigration]:
    """Migrations already applied to this database, oldest first."""
    _ensure_schema_version(conn)
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_version ORDER BY version"
    ).fetchall()
    return [
        AppliedMigration(
            version=row["version"],
            name=row["name"],
            checksum=row["checksum"],
            applied_at=row["applied_at"],
        )
        for row in rows
    ]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply every pending migration in order. Returns the versions applied.

    Verifies that already-applied migrations still match their recorded
    checksums before applying anything, so an edited migration fails loudly
    instead of silently diverging from databases that already ran it.
    """
    _ensure_schema_version(conn)
    applied = {m.version: m for m in schema_status(conn)}
    available = _discover_migrations()

    for migration in available:
        previous = applied.get(migration.version)
        if previous is not None and previous.checksum != migration.checksum:
            raise MigrationTamperedError(
                f"Migration {migration.version:04d}_{migration.name}.sql has changed since "
                f"it was applied ({previous.applied_at}).\n"
                f"  recorded: {previous.checksum}\n"
                f"  on disk:  {migration.checksum}\n"
                "Applied migrations are immutable -- revert the edit and add a new "
                "migration instead."
            )

    newly_applied: list[int] = []
    for migration in available:
        if migration.version in applied:
            continue
        with _immediate(conn):
            conn.executescript(migration.sql)
            conn.execute(
                "INSERT INTO schema_version (version, name, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    migration.version,
                    migration.name,
                    migration.checksum,
                    datetime.now(UTC).isoformat(),
                ),
            )
        newly_applied.append(migration.version)

    return newly_applied


def put_blob(conn: sqlite3.Connection, data: bytes) -> str:
    """Store bytes by content hash and return the hash. Idempotent.

    Does not commit -- the caller owns the transaction, so a batch of blobs and
    the rows referencing them land together.
    """
    digest = hashlib.sha256(data).hexdigest()
    conn.execute(
        "INSERT INTO blobs (sha256, size, content) VALUES (?, ?, ?) ON CONFLICT(sha256) DO NOTHING",
        (digest, len(data), data),
    )
    return digest


def get_blob(conn: sqlite3.Connection, sha256: str) -> bytes | None:
    """Retrieve blob content by hash, or None if absent."""
    row = conn.execute("SELECT content FROM blobs WHERE sha256 = ?", (sha256,)).fetchone()
    return row["content"] if row is not None else None
