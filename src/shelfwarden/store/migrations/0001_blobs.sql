-- Content-addressed blob store.
--
-- Large payloads -- assembled contexts, raw model responses, tool results,
-- evidence bodies -- live here keyed by content hash and are referenced by hash
-- from the tables that arrive in roadmap steps 1.4 and 1.5. This deduplicates
-- the system prompt that repeats across every step of every run, and makes
-- "the same context" a checkable identity rather than a judgement call.

CREATE TABLE blobs (
    sha256     TEXT PRIMARY KEY,
    size       INTEGER NOT NULL,
    content    BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
