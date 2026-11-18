"""SQLite connection, schema and the global writer lock.

Concurrency model
-----------------
SQLite runs in WAL mode. All *mutating* operations (import, certificate
changes, publish) hold :data:`writer_lock` (an ``RLock``) and run inside a
single ``BEGIN IMMEDIATE`` transaction, so they are fully serialized and each
one sees one committed snapshot. Read-only endpoints take short-lived
autocommit connections and never block behind a writer.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

# One writer (and therefore one publish computation) at a time. RLock so a
# service function may call helper functions that also take the lock.
writer_lock = threading.RLock()

_DB_PATH = os.environ.get("CAL_DB_PATH", os.path.join("data", "calibration.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data_version INTEGER NOT NULL DEFAULT 0
);
INSERT OR IGNORE INTO meta(id, data_version) VALUES (1, 0);

CREATE TABLE IF NOT EXISTS readings (
    probe_id      TEXT NOT NULL,
    minute_utc    INTEGER NOT NULL,
    temperature_c REAL NOT NULL,
    data_version  INTEGER NOT NULL,
    PRIMARY KEY (probe_id, minute_utc)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS calibrations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_id     TEXT NOT NULL,
    valid_from   INTEGER NOT NULL,
    valid_to     INTEGER NOT NULL,
    offset_c     REAL NOT NULL,
    data_version INTEGER NOT NULL,
    CHECK (valid_from < valid_to)
);
CREATE INDEX IF NOT EXISTS idx_cal_probe_span
    ON calibrations(probe_id, valid_from, valid_to);

CREATE TABLE IF NOT EXISTS publications (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    probe_id     TEXT NOT NULL,
    day          INTEGER NOT NULL,
    data_version INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('serving', 'stale', 'superseded')),
    payload      TEXT NOT NULL,
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_pub_lookup
    ON publications(probe_id, day, status);
CREATE INDEX IF NOT EXISTS idx_pub_version ON publications(data_version);
"""


def get_db_path() -> str:
    return _DB_PATH


def configure(db_path: str) -> None:
    """Override the database path (used by tests)."""
    global _DB_PATH
    _DB_PATH = db_path


def connect() -> sqlite3.Connection:
    """Open a WAL-mode connection with foreign keys and dict-like rows."""
    os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(_DB_PATH, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx() -> Iterator[sqlite3.Connection]:
    """Serialize writers and open an immediate write transaction.

    Any exception rolls the transaction back; state (including
    ``data_version`` and stale flags) is then exactly as before the call.
    """
    with writer_lock:
        conn = connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def current_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT data_version FROM meta WHERE id = 1").fetchone()
    return int(row["data_version"])


def bump_version(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE meta SET data_version = data_version + 1 WHERE id = 1")
    return current_version(conn)


def mark_days_stale(conn: sqlite3.Connection, probe_id: str, days: list[int]) -> int:
    """Flip the serving publication of each (probe, day) to ``stale``.

    Returns the number of publications marked. Days without a serving
    publication are simply absent from the list, so nothing changes for them.
    """
    changed = 0
    for day in sorted(set(days)):
        cur = conn.execute(
            "UPDATE publications SET status = 'stale' "
            "WHERE probe_id = ? AND day = ? AND status = 'serving'",
            (probe_id, day),
        )
        changed += cur.rowcount
    return changed


def stale_days_for_minutes(
    conn: sqlite3.Connection, probe_id: str, minutes: list[int]
) -> list[int]:
    """UTC days of *minutes* that currently have a serving publication."""
    if not minutes:
        return []
    days = sorted({m // 1440 for m in minutes})
    placeholders = ",".join("?" for _ in days)
    rows = conn.execute(
        f"SELECT DISTINCT day FROM publications "
        f"WHERE probe_id = ? AND status = 'serving' AND day IN ({placeholders})",
        [probe_id, *days],
    ).fetchall()
    return [int(r["day"]) for r in rows]


def stale_days_for_span(
    conn: sqlite3.Connection, probe_id: str, start_minute: int, end_minute: int
) -> list[int]:
    """Serving-publication days overlapped by a half-open minute interval."""
    if end_minute <= start_minute:
        return []
    first_day = start_minute // 1440
    last_day = (end_minute - 1) // 1440
    placeholders = ",".join("?" for _ in range(first_day, last_day + 1))
    rows = conn.execute(
        f"SELECT DISTINCT day FROM publications "
        f"WHERE probe_id = ? AND status = 'serving' "
        f"AND day BETWEEN ? AND ?",
        (probe_id, first_day, last_day),
    ).fetchall()
    return [int(r["day"]) for r in rows]
