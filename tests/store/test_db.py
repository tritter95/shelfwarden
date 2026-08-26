import sqlite3

import pytest

from shelfwarden.store import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "store.db")
    yield connection
    connection.close()


def test_connect_sets_required_pragmas(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert conn.autocommit is False


def test_wal_cannot_be_set_inside_a_transaction(tmp_path):
    """Pin the reason connect() sets pragmas before switching off autocommit.

    `autocommit=False` opens a transaction immediately, and journal_mode cannot
    change inside one. Doing these two steps in the obvious order fails.
    """
    naive = sqlite3.connect(tmp_path / "naive.db", autocommit=False)
    try:
        assert naive.in_transaction
        with pytest.raises(sqlite3.OperationalError, match="wal mode"):
            naive.execute("PRAGMA journal_mode=WAL")
    finally:
        naive.close()


def test_journal_mode_persists_but_foreign_keys_do_not(tmp_path):
    """WAL is a property of the file; foreign_keys is per-connection."""
    path = tmp_path / "store.db"
    db.connect(path).close()

    raw = sqlite3.connect(path)
    try:
        assert raw.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    finally:
        raw.close()


def test_foreign_keys_are_enforced(conn):
    with db.transaction(conn):
        conn.execute("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
    with pytest.raises(sqlite3.IntegrityError), db.transaction(conn):
        conn.execute("INSERT INTO child (id, parent_id) VALUES (1, 999)")


def test_migrate_applies_pending_then_is_idempotent(conn):
    assert db.migrate(conn) == [1]
    assert db.migrate(conn) == []


def test_migrate_creates_the_blobs_table(conn):
    db.migrate(conn)
    tables = {
        row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"blobs", "schema_version"} <= tables


def test_schema_status_records_the_applied_migration(conn):
    db.migrate(conn)
    (applied,) = db.schema_status(conn)
    assert applied.version == 1
    assert applied.name == "blobs"
    assert len(applied.checksum) == 64
    assert applied.applied_at


def test_schema_status_is_empty_before_migrating(conn):
    assert db.schema_status(conn) == []


def test_editing_an_applied_migration_is_detected(conn, monkeypatch):
    db.migrate(conn)
    tampered = [
        db.Migration(
            version=original.version,
            name=original.name,
            sql=original.sql + "\n-- someone edited this\n",
            checksum="0" * 64,
        )
        for original in db._discover_migrations()
    ]
    monkeypatch.setattr(db, "_discover_migrations", lambda: tampered)

    with pytest.raises(db.MigrationTamperedError, match="immutable"):
        db.migrate(conn)


def test_badly_named_migration_is_rejected(conn, monkeypatch):
    class FakeEntry:
        name = "oops.sql"

        def read_text(self, encoding="utf-8"):
            return ""

    class FakePackage:
        def iterdir(self):
            return [FakeEntry()]

    monkeypatch.setattr(db.resources, "files", lambda _: FakePackage())
    with pytest.raises(db.MigrationNameError, match=r"NNNN_name\.sql"):
        db._discover_migrations()


def test_transaction_rolls_back_on_error(conn):
    db.migrate(conn)
    with pytest.raises(RuntimeError), db.transaction(conn):
        db.put_blob(conn, b"discard me")
        raise RuntimeError("boom")
    assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 0


def test_put_blob_round_trips_and_deduplicates(conn):
    db.migrate(conn)
    with db.transaction(conn):
        first = db.put_blob(conn, b"hello")
        second = db.put_blob(conn, b"hello")

    assert first == second
    assert db.get_blob(conn, first) == b"hello"
    assert conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0] == 1
    assert conn.execute("SELECT size FROM blobs").fetchone()[0] == 5


def test_get_blob_returns_none_when_absent(conn):
    db.migrate(conn)
    assert db.get_blob(conn, "0" * 64) is None
