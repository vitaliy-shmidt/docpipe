"""Small SQLite helpers shared by the telemetry and the auth store."""

from __future__ import annotations

import sqlite3
import time


def enable_wal(conn: sqlite3.Connection, timeout_seconds: float) -> None:
    """Switch the database to WAL, retrying while another connection holds it.

    Switching the journal mode of a fresh file needs an exclusive lock, and
    SQLite does not always run the busy handler for it: several Uvicorn
    workers opening a new data directory at the same moment got an immediate
    "database is locked" (Phase M7 load test). Retries are bounded by the
    store's busy timeout; any other error is raised unchanged.
    """
    deadline = time.monotonic() + timeout_seconds
    delay = 0.01
    while True:
        try:
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() == "wal":
                return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) and "busy" not in str(exc):
                raise
        if time.monotonic() >= deadline:
            raise sqlite3.OperationalError("database is locked")
        time.sleep(delay)
        delay = min(delay * 2, 0.2)
