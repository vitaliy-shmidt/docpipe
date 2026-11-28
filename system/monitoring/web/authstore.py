"""Security store of the monitoring web interface (separate SQLite file).

Deliberately separate from the telemetry store (system/monitoring/store.py):

- Telemetry is fail-open (losses are counted, the API never waits); this
  store is fail-closed: every error propagates and the caller denies access.
- Telemetry retention and storage-pressure pruning can never touch sessions,
  rate limits, replay counters or audit records.
- Own size limit (`auth_db_max_bytes`, enforced by SQLite's max_page_count);
  when full, logins fail closed.
- Migrating it never touches existing telemetry data.

Stored: SHA-256 hashes of session and pending-MFA tokens (never the tokens),
usernames from the users file, HMAC-keyed hashes of rate-limit subjects
(client IP, typed username - so attacker-typed strings are never stored),
the last accepted TOTP counter per user (never codes), and minimal audit
records. No passwords, TOTP codes, raw session ids or customer API keys.

Multi-worker safety: every check-and-change runs in one BEGIN IMMEDIATE
transaction, so SQLite's write lock serializes concurrent workers.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from system.monitoring.sqlite_util import enable_wal

SCHEMA_VERSION = 1
AUTH_DB_FILENAME = "monitor-auth.db"
BUSY_TIMEOUT_SECONDS = 5.0
MAX_PENDING_MFA = 1000
AUDIT_DEDUPE_MS = 60_000

AUDIT_EVENTS = frozenset(
    {
        "login_success",
        "login_failed",
        "mfa_failed",
        "totp_replay",
        "lockout",
        "logout",
        "session_expired",
        "session_revoked",
        "access_denied",
        "csrf_failed",
        "security_config_error",
        "login_throttled",
    }
)

_MIGRATION_1 = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value BLOB NOT NULL)",
    """CREATE TABLE sessions (
        id_hash TEXT PRIMARY KEY, username TEXT NOT NULL, fingerprint TEXT NOT NULL,
        created_ms INTEGER NOT NULL, last_activity_ms INTEGER NOT NULL,
        absolute_expiry_ms INTEGER NOT NULL)""",
    "CREATE INDEX ix_sessions_user ON sessions(username, created_ms)",
    """CREATE TABLE pending_mfa (
        id_hash TEXT PRIMARY KEY, username TEXT NOT NULL, fingerprint TEXT NOT NULL,
        created_ms INTEGER NOT NULL, expires_ms INTEGER NOT NULL, attempts INTEGER NOT NULL)""",
    """CREATE TABLE rate_limits (
        scope TEXT NOT NULL, subject TEXT NOT NULL, window_start_ms INTEGER NOT NULL,
        failures INTEGER NOT NULL, locked_until_ms INTEGER NOT NULL, PRIMARY KEY (scope, subject))""",
    "CREATE TABLE totp_last (username TEXT PRIMARY KEY, last_counter INTEGER NOT NULL)",
    """CREATE TABLE audit (
        id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, last_ts_ms INTEGER NOT NULL, event TEXT NOT NULL,
        username TEXT, ip TEXT, reason TEXT, session_ref TEXT, repeat_count INTEGER NOT NULL)""",
    "CREATE INDEX ix_audit_ts ON audit(ts_ms)",
]
MIGRATIONS = {1: _MIGRATION_1}

# Accept a TOTP time-step counter only if it is newer than every accepted one.
_ACCEPT_COUNTER = (
    "INSERT INTO totp_last (username, last_counter) VALUES (?, ?) ON CONFLICT (username) DO UPDATE "
    "SET last_counter = excluded.last_counter WHERE excluded.last_counter > totp_last.last_counter"
)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class SessionRow:
    id_hash: str
    username: str
    fingerprint: str
    created_ms: int
    last_activity_ms: int
    absolute_expiry_ms: int


@dataclass(frozen=True)
class PendingRow:
    id_hash: str
    username: str
    fingerprint: str
    expires_ms: int
    attempts: int


class AuthStore:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self._max_bytes = max_bytes
        self._conn: sqlite3.Connection | None = None
        self._subject_key: bytes | None = None
        # One connection per process, used from the thread pool: every
        # transaction and read holds this lock, so they never interleave on
        # the connection. Across worker processes SQLite's own locking
        # (BEGIN IMMEDIATE) serializes.
        self._lock = threading.RLock()

    # --- lifecycle -------------------------------------------------------------

    def open(self) -> None:
        if self._conn is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None, check_same_thread=False
        )
        try:
            if os.name == "posix":
                os.chmod(self.path, 0o600)
            enable_wal(conn, BUSY_TIMEOUT_SECONDS)
            conn.execute("PRAGMA synchronous = FULL")
            conn.execute("PRAGMA temp_store = MEMORY")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count = {max(64, self._max_bytes // page_size)}")
            conn.execute(f"PRAGMA journal_size_limit = {min(self._max_bytes // 4, 16 * 1024 * 1024)}")
            self._migrate(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM meta WHERE key = 'subject_key'").fetchone()
            if row is None:
                key = secrets.token_bytes(32)
                conn.execute("INSERT INTO meta (key, value) VALUES ('subject_key', ?)", (key,))
            else:
                key = bytes(row[0])
            conn.execute("COMMIT")
        except Exception:
            conn.close()
            raise
        self._subject_key = key
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise sqlite3.DatabaseError("auth database was created by a newer DocPipe version")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Several workers start at once: another one may have migrated
                # while this one waited for the write lock (found in the M7 load
                # test with 4 Uvicorn workers on a fresh data directory).
                if conn.execute("PRAGMA user_version").fetchone()[0] >= target:
                    conn.execute("COMMIT")
                    continue
                for statement in MIGRATIONS[target]:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise sqlite3.OperationalError("auth store is not open")
        return self._conn

    def _tx(self, fn):
        with self._lock:
            conn = self._db()
            conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(conn)
                conn.execute("COMMIT")
                return result
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise

    def _read(self, sql: str, params=()):
        with self._lock:
            return self._db().execute(sql, params).fetchone()

    def schema_version(self) -> int:
        return self._db().execute("PRAGMA user_version").fetchone()[0]

    def subject(self, value: str) -> str:
        """Keyed hash of a rate-limit subject (IP or typed username)."""
        if self._subject_key is None:
            raise sqlite3.OperationalError("auth store is not open")
        return hmac.new(self._subject_key, value.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()

    # --- audit -----------------------------------------------------------------

    @staticmethod
    def _audit(
        conn,
        now_ms: int,
        event: str,
        *,
        username=None,
        ip=None,
        reason=None,
        session_ref=None,
        dedupe: bool = False,
    ) -> None:
        if event not in AUDIT_EVENTS:
            raise ValueError("unknown audit event")
        if dedupe:
            row = conn.execute(
                "SELECT id FROM audit WHERE event = ? AND ip IS ? AND reason IS ? AND username IS ? "
                "AND last_ts_ms >= ? ORDER BY id DESC LIMIT 1",
                (event, ip, reason, username, now_ms - AUDIT_DEDUPE_MS),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE audit SET repeat_count = repeat_count + 1, last_ts_ms = ? WHERE id = ?",
                    (now_ms, row[0]),
                )
                return
        conn.execute(
            "INSERT INTO audit (ts_ms, last_ts_ms, event, username, ip, reason, session_ref, repeat_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1)",
            (now_ms, now_ms, event, username, ip, reason, session_ref),
        )

    def audit(self, now_ms: int, event: str, **fields) -> None:
        self._tx(lambda conn: self._audit(conn, now_ms, event, **fields))

    # --- rate limits ---------------------------------------------------------------

    def locked(self, scope: str, subject: str, now_ms: int) -> bool:
        row = self._read(
            "SELECT locked_until_ms FROM rate_limits WHERE scope = ? AND subject = ?", (scope, subject)
        )
        return row is not None and row[0] > now_ms

    def record_failure(
        self, scope: str, subject: str, now_ms: int, *, limit: int, window_ms: int, lockout_ms: int
    ) -> tuple[bool, bool]:
        """Count one failure; returns (locked now, locked by this failure)."""

        def write(conn):
            row = conn.execute(
                "SELECT window_start_ms, failures, locked_until_ms FROM rate_limits "
                "WHERE scope = ? AND subject = ?",
                (scope, subject),
            ).fetchone()
            if row is None or now_ms - row[0] >= window_ms:
                start, failures, locked_until = now_ms, 0, row[2] if row else 0
            else:
                start, failures, locked_until = row
            failures += 1
            newly_locked = failures >= limit and locked_until <= now_ms
            if failures >= limit:
                locked_until = max(locked_until, now_ms + lockout_ms)
            conn.execute(
                "INSERT INTO rate_limits (scope, subject, window_start_ms, failures, locked_until_ms) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT (scope, subject) DO UPDATE SET "
                "window_start_ms = excluded.window_start_ms, failures = excluded.failures, "
                "locked_until_ms = excluded.locked_until_ms",
                (scope, subject, start, failures, locked_until),
            )
            return locked_until > now_ms, newly_locked

        return self._tx(write)

    def reset_failures(self, scope: str, subject: str, now_ms: int) -> None:
        """After a successful login: forget counted failures (never an active lock)."""
        self._tx(
            lambda conn: conn.execute(
                "DELETE FROM rate_limits WHERE scope = ? AND subject = ? AND locked_until_ms <= ?",
                (scope, subject, now_ms),
            )
        )

    # --- MFA pending state -----------------------------------------------------------

    def create_pending(self, token: str, username: str, fingerprint: str, now_ms: int, ttl_ms: int) -> None:
        def write(conn):
            conn.execute("DELETE FROM pending_mfa WHERE expires_ms <= ? OR username = ?", (now_ms, username))
            count = conn.execute("SELECT COUNT(*) FROM pending_mfa").fetchone()[0]
            if count >= MAX_PENDING_MFA:
                raise sqlite3.OperationalError("too many pending MFA states")
            conn.execute(
                "INSERT INTO pending_mfa VALUES (?, ?, ?, ?, ?, 0)",
                (token_hash(token), username, fingerprint, now_ms, now_ms + ttl_ms),
            )

        self._tx(write)

    def get_pending(self, token: str, now_ms: int) -> PendingRow | None:
        row = self._read(
            "SELECT id_hash, username, fingerprint, expires_ms, attempts FROM pending_mfa WHERE id_hash = ?",
            (token_hash(token),),
        )
        if row is None or row[3] <= now_ms:
            return None
        return PendingRow(*row)

    def pending_attempt(self, token: str, now_ms: int, max_attempts: int) -> PendingRow | None:
        """Atomically count one MFA attempt; None if the state is gone,
        expired or out of attempts (then it is deleted)."""
        id_hash = token_hash(token)

        def write(conn):
            row = conn.execute(
                "SELECT id_hash, username, fingerprint, expires_ms, attempts FROM pending_mfa "
                "WHERE id_hash = ?",
                (id_hash,),
            ).fetchone()
            if row is None:
                return None
            if row[3] <= now_ms or row[4] >= max_attempts:
                conn.execute("DELETE FROM pending_mfa WHERE id_hash = ?", (id_hash,))
                return None
            conn.execute("UPDATE pending_mfa SET attempts = attempts + 1 WHERE id_hash = ?", (id_hash,))
            return PendingRow(row[0], row[1], row[2], row[3], row[4] + 1)

        return self._tx(write)

    def delete_pending(self, token: str) -> None:
        self._tx(lambda conn: conn.execute("DELETE FROM pending_mfa WHERE id_hash = ?", (token_hash(token),)))

    # --- TOTP replay protection ------------------------------------------------------------

    def accept_totp_counter(self, username: str, counter: int) -> bool:
        """Atomically accept `counter` for `username` once: True only if it is
        greater than every counter accepted before (codes are never stored)."""

        def write(conn):
            cur = conn.execute(
                _ACCEPT_COUNTER,
                (username, counter),
            )
            return cur.rowcount == 1

        return self._tx(write)

    # --- sessions ------------------------------------------------------------------------

    def complete_login(
        self,
        *,
        pending_token: str,
        session_token: str,
        username: str,
        fingerprint: str,
        counter: int,
        now_ms: int,
        absolute_ms: int,
        max_sessions: int,
        ip: str | None,
        old_session_token: str | None,
    ) -> bool:
        """One transaction: accept the TOTP counter (replay check), consume the
        pending state, create the session, enforce the per-user session
        limit and write the audit record. False = replay (nothing created)."""

        def write(conn):
            cur = conn.execute(
                _ACCEPT_COUNTER,
                (username, counter),
            )
            if cur.rowcount != 1:
                self._audit(conn, now_ms, "totp_replay", username=username, ip=ip)
                return False
            conn.execute("DELETE FROM pending_mfa WHERE id_hash = ?", (token_hash(pending_token),))
            if old_session_token:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (token_hash(old_session_token),))
            id_hash = token_hash(session_token)
            conn.execute(
                "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)",
                (id_hash, username, fingerprint, now_ms, now_ms, now_ms + absolute_ms),
            )
            conn.execute(
                "DELETE FROM sessions WHERE username = ? AND id_hash NOT IN (SELECT id_hash FROM sessions "
                "WHERE username = ? ORDER BY created_ms DESC, rowid DESC LIMIT ?)",
                (username, username, max_sessions),
            )
            self._audit(conn, now_ms, "login_success", username=username, ip=ip, session_ref=id_hash[:12])
            return True

        return self._tx(write)

    def get_session(self, token: str) -> SessionRow | None:
        row = self._read(
            "SELECT id_hash, username, fingerprint, created_ms, last_activity_ms, absolute_expiry_ms "
            "FROM sessions WHERE id_hash = ?",
            (token_hash(token),),
        )
        return SessionRow(*row) if row else None

    def touch_session(self, id_hash: str, now_ms: int) -> None:
        self._tx(
            lambda conn: conn.execute(
                "UPDATE sessions SET last_activity_ms = ? WHERE id_hash = ? AND last_activity_ms < ?",
                (now_ms, id_hash, now_ms),
            )
        )

    def end_session(
        self,
        id_hash: str,
        now_ms: int,
        event: str,
        *,
        username: str | None,
        ip: str | None,
        reason: str | None = None,
    ) -> None:
        def write(conn):
            conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
            self._audit(
                conn, now_ms, event, username=username, ip=ip, reason=reason, session_ref=id_hash[:12]
            )

        self._tx(write)

    def revoke_user_sessions(self, username: str) -> int:
        return self._tx(
            lambda conn: conn.execute("DELETE FROM sessions WHERE username = ?", (username,)).rowcount
        )

    # --- retention -------------------------------------------------------------------------

    def cleanup(
        self, now_ms: int, *, idle_ms: int, window_ms: int, audit_days: int, max_audit_rows: int
    ) -> None:
        def write(conn):
            expired = conn.execute(
                "SELECT id_hash, username, absolute_expiry_ms <= ? FROM sessions "
                "WHERE absolute_expiry_ms <= ? OR last_activity_ms + ? <= ?",
                (now_ms, now_ms, idle_ms, now_ms),
            ).fetchall()
            for id_hash, username, absolute in expired:
                conn.execute("DELETE FROM sessions WHERE id_hash = ?", (id_hash,))
                self._audit(
                    conn,
                    now_ms,
                    "session_expired",
                    username=username,
                    reason="absolute" if absolute else "idle",
                    session_ref=id_hash[:12],
                )
            conn.execute("DELETE FROM pending_mfa WHERE expires_ms <= ?", (now_ms,))
            conn.execute(
                "DELETE FROM rate_limits WHERE locked_until_ms <= ? AND window_start_ms + ? <= ?",
                (now_ms, window_ms, now_ms),
            )
            conn.execute("DELETE FROM audit WHERE ts_ms < ?", (now_ms - audit_days * 86_400_000,))
            excess = conn.execute("SELECT COUNT(*) FROM audit").fetchone()[0] - max_audit_rows
            if excess > 0:
                conn.execute(
                    "DELETE FROM audit WHERE id IN (SELECT id FROM audit ORDER BY id LIMIT ?)", (excess,)
                )

        self._tx(write)

    # --- read-only view for the security dashboard (Phase M4) ------------------------------

    def read_security(
        self,
        start_ms: int,
        end_ms: int,
        *,
        events: list[str],
        limit: int,
        cursor: tuple[int, int] | None,
        bucket_ms: int,
        first_bucket: int,
        username: str,
        current_session: str,
        now_ms: int,
        idle_ms: int,
    ) -> dict:
        """Audit counts, timeline rows and one page of recent events in
        [start_ms, end_ms), plus the signed-in user's own sessions. Session
        hashes are reduced to the 12-character reference; nothing secret."""
        marks = ", ".join("?" * len(events)) or "NULL"
        with self._lock:
            conn = self._db()
            counts = dict(
                conn.execute(
                    "SELECT event, SUM(repeat_count) FROM audit WHERE ts_ms >= ? AND ts_ms < ? GROUP BY "
                    "event",
                    (start_ms, end_ms),
                ).fetchall()
            )
            timeline = conn.execute(
                "SELECT event, (ts_ms - ?) / ?, SUM(repeat_count) FROM audit WHERE ts_ms >= ? AND ts_ms < ? "
                "GROUP BY 1, 2",
                (first_bucket, bucket_ms, start_ms, end_ms),
            ).fetchall()
            cursor_sql, cursor_args = (
                (" AND (ts_ms < ? OR (ts_ms = ? AND id < ?))", [cursor[0], cursor[0], cursor[1]])
                if cursor
                else ("", [])
            )
            rows = conn.execute(
                "SELECT id, ts_ms, last_ts_ms, event, username, ip, reason, session_ref, repeat_count FROM "
                "audit "
                f"WHERE ts_ms >= ? AND ts_ms < ? AND event IN ({marks}){cursor_sql} "
                "ORDER BY ts_ms DESC, id DESC LIMIT ?",
                (start_ms, end_ms, *events, *cursor_args, limit + 1),
            ).fetchall()
            current_hash = token_hash(current_session) if current_session else ""
            sessions = conn.execute(
                "SELECT id_hash, created_ms, last_activity_ms, absolute_expiry_ms FROM sessions WHERE "
                "username = ? "
                "ORDER BY created_ms DESC",
                (username,),
            ).fetchall()
        page = rows[:limit]
        return {
            "counts": counts,
            "timeline_rows": timeline,
            "events": [
                {
                    "id": r[0],
                    "ts_ms": r[1],
                    "last_ts_ms": r[2],
                    "event": r[3],
                    "username": r[4],
                    "ip": r[5],
                    "reason": r[6],
                    "session_ref": r[7],
                    "repeat_count": r[8],
                }
                for r in page
            ],
            "next_cursor": f"{page[-1][1]}.{page[-1][0]}" if len(rows) > limit and page else None,
            "sessions": [
                {
                    "session_ref": h[:12],
                    "created_ms": c,
                    "last_activity_ms": a,
                    "idle_expires_ms": min(a + idle_ms, x),
                    "absolute_expires_ms": x,
                    "current": hmac.compare_digest(h, current_hash),
                }
                for h, c, a, x in sessions
            ],
        }
