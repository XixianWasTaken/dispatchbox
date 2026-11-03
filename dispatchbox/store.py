"""SQLite persistence. All access goes through one connection guarded by the hub's lock."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    received_at REAL NOT NULL,
    source TEXT NOT NULL,
    type TEXT NOT NULL,
    subject TEXT NOT NULL,
    summary TEXT NOT NULL,
    link TEXT NOT NULL,
    payload TEXT NOT NULL,
    dedupe_key TEXT NOT NULL,
    status TEXT NOT NULL,          -- routed | coalesced | duplicate | unmatched
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS events_dedupe ON events(dedupe_key, received_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    rule TEXT NOT NULL,
    worker_pattern TEXT NOT NULL,
    group_key TEXT NOT NULL,
    concurrency INTEGER NOT NULL,
    spec TEXT NOT NULL,
    status TEXT NOT NULL,          -- queued | running | succeeded | failed | lost | awaiting_decision | decided
    created_at REAL NOT NULL,
    claimed_by TEXT,
    claimed_at REAL,
    heartbeat_at REAL,
    finished_at REAL,
    exit_code INTEGER,
    summary TEXT,
    session_id TEXT,
    prompt TEXT,
    output_tail TEXT,
    decision_request TEXT,
    decision TEXT,
    parent_run INTEGER,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    attempt INTEGER NOT NULL DEFAULT 1,
    retry_of INTEGER
);
CREATE INDEX IF NOT EXISTS runs_status ON runs(status, id);

CREATE TABLE IF NOT EXISTS run_events (
    run_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (run_id, event_id)
);
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Columns added after the first release. Older databases get them on startup.
_ADDED_RUN_COLUMNS = {
    "cost_usd": "REAL",
    "input_tokens": "INTEGER",
    "output_tokens": "INTEGER",
    "attempt": "INTEGER NOT NULL DEFAULT 1",
    "retry_of": "INTEGER",
}


def _migrate(conn: sqlite3.Connection) -> None:
    have = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    for name, decl in _ADDED_RUN_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {name} {decl}")


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for key in ("payload", "spec", "decision_request", "decision"):
        if key in d and isinstance(d[key], str) and d[key]:
            try:
                d[key] = json.loads(d[key])
            except json.JSONDecodeError:
                pass
    return d
