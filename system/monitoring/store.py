"""SQLite store for monitoring data.

- One database file in the configured data directory (created only when
  monitoring is enabled), WAL mode, `PRAGMA user_version` schema versioning
  with forward-only migrations.
- Written by the collector thread only, in batches (one transaction per
  flush). Rollups are maintained additively in the same transaction as the
  raw rows they summarize, so they are exact even when several worker
  processes or late (retried) batches write the same time bucket.
- Bounded: `max_page_count` caps the main file, `journal_size_limit` and
  regular TRUNCATE checkpoints bound the -wal file, `temp_store=MEMORY`
  avoids SQLite temp files. The storage check counts db + -wal + -shm.
- All timestamps are UTC epoch milliseconds.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from system.monitoring.recorder import Batch, QualityCounters, minute_of
from system.monitoring.settings import MIB, MonitoringSettings
from system.monitoring.sqlite_util import enable_wal
from system.monitoring.stats import (
    BUCKET_COLUMNS,
    BUCKET_COUNT,
    PERCENTILES,
    bucket_index,
    exact_percentile,
    histogram_percentile,
)

SCHEMA_VERSION = 3
MINUTE_MS = 60_000
HOUR_MS = 3_600_000
DAY_MS = 86_400_000
RESOLUTIONS = (MINUTE_MS, HOUR_MS)
BUSY_TIMEOUT_SECONDS = 2.0
DELETE_CHUNK = 5_000

_HIST_DDL = ", ".join(f"{c} INTEGER NOT NULL DEFAULT 0" for c in BUCKET_COLUMNS)

_MIGRATION_1 = [
    """CREATE TABLE instances (
        instance_id TEXT PRIMARY KEY, pid INTEGER NOT NULL, started_ms INTEGER NOT NULL,
        last_heartbeat_ms INTEGER NOT NULL, stopped_ms INTEGER, app_version TEXT NOT NULL,
        build_commit TEXT, image_tag TEXT, build_source_state TEXT NOT NULL, build_local_patches TEXT)""",
    """CREATE TABLE request_events (
        id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, ts_ms INTEGER NOT NULL,
        duration_ms REAL NOT NULL, instance_id TEXT NOT NULL, client_id TEXT, route TEXT NOT NULL,
        method TEXT NOT NULL, service TEXT NOT NULL, status_code INTEGER NOT NULL, error_code TEXT,
        bytes_in INTEGER NOT NULL, bytes_out INTEGER NOT NULL, documents INTEGER NOT NULL, pages INTEGER,
        ai_provider TEXT, ai_model TEXT, ai_calls INTEGER NOT NULL, tokens_in INTEGER, tokens_out INTEGER,
        steps_truncated INTEGER NOT NULL)""",
    "CREATE INDEX ix_request_events_ts ON request_events(ts_ms)",
    "CREATE INDEX ix_request_events_client_ts ON request_events(client_id, ts_ms)",
    """CREATE TABLE request_steps (
        id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, ts_ms INTEGER NOT NULL, seq INTEGER NOT NULL,
        route TEXT NOT NULL, step TEXT NOT NULL, outcome TEXT NOT NULL, duration_ms REAL NOT NULL,
        ollama_total_ms REAL, ollama_load_ms REAL, ollama_prompt_eval_ms REAL, ollama_eval_ms REAL,
        tokens_in INTEGER, tokens_out INTEGER, repair_used INTEGER NOT NULL, ai_model TEXT)""",
    "CREATE INDEX ix_request_steps_ts ON request_steps(ts_ms)",
    "CREATE INDEX ix_request_steps_request ON request_steps(request_id)",
    f"""CREATE TABLE rollup_requests (
        resolution_ms INTEGER NOT NULL, bucket_ms INTEGER NOT NULL, client_id TEXT NOT NULL,
        route TEXT NOT NULL, method TEXT NOT NULL, service TEXT NOT NULL, status_code INTEGER NOT NULL,
        error_code TEXT NOT NULL, ai_model TEXT NOT NULL,
        count INTEGER NOT NULL, dur_sum REAL NOT NULL, dur_min REAL NOT NULL, dur_max REAL NOT NULL,
        bytes_in INTEGER NOT NULL, bytes_out INTEGER NOT NULL, documents INTEGER NOT NULL,
        pages INTEGER NOT NULL, pages_known INTEGER NOT NULL, ai_calls INTEGER NOT NULL,
        tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL, tokens_known INTEGER NOT NULL,
        {_HIST_DDL},
        PRIMARY KEY (resolution_ms, bucket_ms, client_id, route, method, service, status_code,
                     error_code, ai_model)) WITHOUT ROWID""",
    f"""CREATE TABLE rollup_steps (
        resolution_ms INTEGER NOT NULL, bucket_ms INTEGER NOT NULL, route TEXT NOT NULL,
        step TEXT NOT NULL, outcome TEXT NOT NULL, ai_model TEXT NOT NULL,
        count INTEGER NOT NULL, dur_sum REAL NOT NULL, dur_min REAL NOT NULL, dur_max REAL NOT NULL,
        ollama_known INTEGER NOT NULL, ollama_load_sum REAL NOT NULL, ollama_prompt_eval_sum REAL NOT NULL,
        ollama_eval_sum REAL NOT NULL, tokens_in INTEGER NOT NULL, tokens_out INTEGER NOT NULL,
        tokens_known INTEGER NOT NULL, {_HIST_DDL},
        PRIMARY KEY (resolution_ms, bucket_ms, route, step, outcome, ai_model)) WITHOUT ROWID""",
    """CREATE TABLE system_samples (
        id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, instance_id TEXT NOT NULL, metric TEXT NOT NULL,
        source TEXT NOT NULL, value REAL, status TEXT NOT NULL, reason TEXT NOT NULL)""",
    "CREATE INDEX ix_system_samples_ts ON system_samples(ts_ms)",
    """CREATE TABLE rollup_system (
        resolution_ms INTEGER NOT NULL, bucket_ms INTEGER NOT NULL, metric TEXT NOT NULL,
        source TEXT NOT NULL, n INTEGER NOT NULL, sum REAL NOT NULL, min REAL NOT NULL, max REAL NOT NULL,
        PRIMARY KEY (resolution_ms, bucket_ms, metric, source)) WITHOUT ROWID""",
    """CREATE TABLE service_probes (
        id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, service TEXT NOT NULL, status TEXT NOT NULL,
        http_status INTEGER, latency_ms REAL, version TEXT)""",
    "CREATE INDEX ix_service_probes_ts ON service_probes(ts_ms)",
    """CREATE TABLE ollama_models (
        id INTEGER PRIMARY KEY, ts_ms INTEGER NOT NULL, name TEXT NOT NULL, installed INTEGER NOT NULL,
        loaded INTEGER NOT NULL, size_bytes INTEGER, size_vram_bytes INTEGER, family TEXT,
        parameter_size TEXT, quantization TEXT, context_length INTEGER, expires_ms INTEGER)""",
    "CREATE INDEX ix_ollama_models_ts ON ollama_models(ts_ms)",
    """CREATE TABLE events (
        id INTEGER PRIMARY KEY, first_ts_ms INTEGER NOT NULL, last_ts_ms INTEGER NOT NULL,
        severity TEXT NOT NULL, code TEXT NOT NULL, service TEXT NOT NULL, client_id TEXT, route TEXT,
        error_code TEXT, request_id TEXT, detail TEXT, instance_id TEXT NOT NULL,
        repeat_count INTEGER NOT NULL)""",
    "CREATE INDEX ix_events_ts ON events(last_ts_ms)",
    """CREATE TABLE data_quality (
        bucket_ms INTEGER NOT NULL, instance_id TEXT NOT NULL, heartbeats INTEGER NOT NULL DEFAULT 0,
        requests_persisted INTEGER NOT NULL DEFAULT 0, requests_dropped_buffer INTEGER NOT NULL DEFAULT 0,
        requests_dropped_store INTEGER NOT NULL DEFAULT 0, events_dropped INTEGER NOT NULL DEFAULT 0,
        steps_truncated INTEGER NOT NULL DEFAULT 0, flush_failures INTEGER NOT NULL DEFAULT 0,
        task_failures INTEGER NOT NULL DEFAULT 0, hook_failures INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (bucket_ms, instance_id)) WITHOUT ROWID""",
    """CREATE TABLE live_requests (
        instance_id TEXT NOT NULL, request_id TEXT NOT NULL, route TEXT NOT NULL, method TEXT NOT NULL,
        service TEXT NOT NULL, started_ms INTEGER NOT NULL, snapshot_ms INTEGER NOT NULL,
        PRIMARY KEY (instance_id, request_id)) WITHOUT ROWID""",
    """CREATE TABLE leases (
        name TEXT PRIMARY KEY, holder TEXT NOT NULL, expires_ms INTEGER NOT NULL)""",
]

# v2 (Phase M4): indexes for the dashboard queries (checked with EXPLAIN QUERY
# PLAN in tests/test_monitor_dashboard_queries.py). Only adds indexes - existing
# data is kept as it is.
_MIGRATION_2 = [
    "CREATE INDEX IF NOT EXISTS ix_request_events_service_ts ON request_events(service, ts_ms)",
    "CREATE INDEX IF NOT EXISTS ix_request_steps_step_ts ON request_steps(step, ts_ms)",
    "CREATE INDEX IF NOT EXISTS ix_system_samples_metric_ts ON system_samples(metric, ts_ms)",
    "CREATE INDEX IF NOT EXISTS ix_service_probes_service_ts ON service_probes(service, ts_ms)",
    "CREATE INDEX IF NOT EXISTS ix_request_events_request_id ON request_events(request_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_service_ts ON events(service, last_ts_ms)",
]

# v3 (Phase M5): paired throughput sums for AI steps and a snapshot table of the
# AI steps currently running in each worker. A token count only enters a pair sum
# together with its own measured duration (> 0) - weighted throughput is then
# sum(tokens) / sum(seconds), never an average of per-call rates. Buckets written
# before v3 keep NULL (= not available) unless the raw rows still cover them
# completely; see _backfill_v3. Later additions to a NULL bucket keep it NULL
# (NULL + x = NULL), so an incomplete bucket is never shown as a partial sum.
_PAIR_COLUMNS = (
    "eval_pair_tokens",
    "eval_pair_ms",
    "eval_pairs",
    "prompt_pair_tokens",
    "prompt_pair_ms",
    "prompt_pairs",
)
BACKFILL_MAX_RAW_ROWS = 2_000_000


def _backfill_v3(conn: sqlite3.Connection) -> None:
    """Fill the pair sums of existing AI step rollups from raw rows - only for
    buckets whose raw rows are all still present (same count), bounded by the
    raw retention and a hard row cap."""
    raw = conn.execute("SELECT COUNT(*) FROM request_steps WHERE step LIKE 'ai.%'").fetchone()[0]
    if raw == 0 or raw > BACKFILL_MAX_RAW_ROWS:
        return
    for resolution in RESOLUTIONS:
        rows = conn.execute(
            "SELECT ts_ms - ts_ms % ?, route, step, outcome, COALESCE(ai_model, ''), COUNT(*), "
            "COALESCE(SUM(CASE WHEN tokens_out IS NOT NULL AND ollama_eval_ms > 0 THEN tokens_out END), 0), "
            "COALESCE(SUM(CASE WHEN tokens_out IS NOT NULL AND ollama_eval_ms > 0 THEN "
            "ollama_eval_ms END), 0), "
            "SUM(CASE WHEN tokens_out IS NOT NULL AND ollama_eval_ms > 0 THEN 1 ELSE 0 END), "
            "COALESCE(SUM(CASE WHEN tokens_in IS NOT NULL AND ollama_prompt_eval_ms > 0 THEN "
            "tokens_in END), 0), "
            "COALESCE(SUM(CASE WHEN tokens_in IS NOT NULL AND ollama_prompt_eval_ms > 0 "
            "THEN ollama_prompt_eval_ms END), 0), "
            "SUM(CASE WHEN tokens_in IS NOT NULL AND ollama_prompt_eval_ms > 0 THEN 1 ELSE 0 END) "
            "FROM request_steps WHERE step LIKE 'ai.%' GROUP BY 1, 2, 3, 4, 5",
            (resolution,),
        ).fetchall()
        conn.executemany(
            "UPDATE rollup_steps SET eval_pair_tokens = ?, eval_pair_ms = ?, eval_pairs = ?, "
            "prompt_pair_tokens = ?, prompt_pair_ms = ?, prompt_pairs = ? WHERE resolution_ms = ? "
            "AND bucket_ms = ? AND route = ? AND step = ? AND outcome = ? AND ai_model = ? AND count = ?",
            [(*r[6:12], resolution, r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows],
        )


_MIGRATION_3 = [
    *(
        f"ALTER TABLE rollup_steps ADD COLUMN {c} {'REAL' if c.endswith('_ms') else 'INTEGER'}"
        for c in _PAIR_COLUMNS
    ),
    """CREATE TABLE live_ai_steps (
        instance_id TEXT NOT NULL, request_id TEXT NOT NULL, client_id TEXT, ai_model TEXT,
        operation TEXT NOT NULL,
        step TEXT NOT NULL, started_ms INTEGER NOT NULL, snapshot_ms INTEGER NOT NULL,
        PRIMARY KEY (instance_id, request_id, step, started_ms)) WITHOUT ROWID""",
    _backfill_v3,
]

MIGRATIONS: dict[int, list] = {1: _MIGRATION_1, 2: _MIGRATION_2, 3: _MIGRATION_3}

_QUALITY_FIELDS = (
    "heartbeats",
    "requests_dropped_buffer",
    "requests_dropped_store",
    "events_dropped",
    "steps_truncated",
    "flush_failures",
    "task_failures",
    "hook_failures",
)


class StoreUnavailable(Exception):
    """The store is not open or a write did not happen. Never raised into a request."""


def _upsert_sql(
    table: str,
    keys: tuple[str, ...],
    adds: tuple[str, ...],
    mins: tuple[str, ...] = (),
    maxs: tuple[str, ...] = (),
) -> str:
    cols = (*keys, *adds, *mins, *maxs)
    updates = [f"{c} = {c} + excluded.{c}" for c in adds]
    updates += [f"{c} = MIN({c}, excluded.{c})" for c in mins]
    updates += [f"{c} = MAX({c}, excluded.{c})" for c in maxs]
    return (
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
        f"ON CONFLICT ({', '.join(keys)}) DO UPDATE SET {', '.join(updates)}"
    )


_REQ_KEYS = (
    "resolution_ms",
    "bucket_ms",
    "client_id",
    "route",
    "method",
    "service",
    "status_code",
    "error_code",
    "ai_model",
)
_REQ_ADDS = (
    "count",
    "dur_sum",
    "bytes_in",
    "bytes_out",
    "documents",
    "pages",
    "pages_known",
    "ai_calls",
    "tokens_in",
    "tokens_out",
    "tokens_known",
    *BUCKET_COLUMNS,
)
_REQ_UPSERT = _upsert_sql("rollup_requests", _REQ_KEYS, _REQ_ADDS, ("dur_min",), ("dur_max",))
_STEP_KEYS = ("resolution_ms", "bucket_ms", "route", "step", "outcome", "ai_model")
_STEP_ADDS = (
    "count",
    "dur_sum",
    "ollama_known",
    "ollama_load_sum",
    "ollama_prompt_eval_sum",
    "ollama_eval_sum",
    "tokens_in",
    "tokens_out",
    "tokens_known",
    *BUCKET_COLUMNS,
    "eval_pair_tokens",
    "eval_pair_ms",
    "eval_pairs",
    "prompt_pair_tokens",
    "prompt_pair_ms",
    "prompt_pairs",
)
_STEP_UPSERT = _upsert_sql("rollup_steps", _STEP_KEYS, _STEP_ADDS, ("dur_min",), ("dur_max",))
_SYS_UPSERT = _upsert_sql(
    "rollup_system", ("resolution_ms", "bucket_ms", "metric", "source"), ("n", "sum"), ("min",), ("max",)
)
_DQ_UPSERT = _upsert_sql(
    "data_quality", ("bucket_ms", "instance_id"), ("requests_persisted", *_QUALITY_FIELDS)
)


def _bucket(ts_ms: int, resolution: int) -> int:
    return ts_ms - ts_ms % resolution


class MonitoringStore:
    def __init__(self, settings: MonitoringSettings) -> None:
        self._settings = settings
        self.path: Path = settings.db_path
        self._conn: sqlite3.Connection | None = None
        max_bytes = settings.storage.max_bytes
        # Room reserved for the -wal/-shm files inside max_bytes.
        self._wal_reserve = min(max(max_bytes // 8, 4 * MIB), 64 * MIB)

    # --- lifecycle -----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._conn is not None

    def open(self) -> None:
        if self._conn is not None:
            return
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        is_new = not self.path.exists()
        conn = sqlite3.connect(
            str(self.path), timeout=BUSY_TIMEOUT_SECONDS, isolation_level=None, check_same_thread=False
        )
        try:
            if os.name == "posix":
                os.chmod(self.path, 0o600)
            if is_new:
                conn.execute("PRAGMA auto_vacuum = INCREMENTAL")
            enable_wal(conn, BUSY_TIMEOUT_SECONDS)
            conn.execute("PRAGMA synchronous = NORMAL")
            conn.execute("PRAGMA temp_store = MEMORY")
            conn.execute("PRAGMA cache_size = -4096")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            conn.execute(f"PRAGMA journal_size_limit = {int(self._wal_reserve)}")
            conn.execute(f"PRAGMA wal_autocheckpoint = {max(100, int(self._wal_reserve // page_size // 2))}")
            max_pages = max(256, int((self._settings.storage.max_bytes - self._wal_reserve) // page_size))
            conn.execute(f"PRAGMA max_page_count = {max_pages}")
            self._migrate(conn)
        except Exception:
            conn.close()
            raise
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()
            self._conn = None

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise StoreUnavailable("monitoring database was created by a newer DocPipe version")
        for target in range(version + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                # Another worker may have migrated while we waited for the lock.
                if conn.execute("PRAGMA user_version").fetchone()[0] >= target:
                    conn.execute("COMMIT")
                    continue
                for statement in MIGRATIONS[target]:
                    if callable(statement):
                        statement(conn)
                    else:
                        conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {target}")
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise StoreUnavailable("monitoring store is not open")
        return self._conn

    def schema_version(self) -> int:
        return self._db().execute("PRAGMA user_version").fetchone()[0]

    # --- size ------------------------------------------------------------------

    def file_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm", "-journal"):
            try:
                total += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return total

    def checkpoint(self) -> None:
        self._db().execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # --- writes ------------------------------------------------------------------

    def _transaction(self, fn):
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

    def register_instance(self, *, instance_id: str, pid: int, now_ms: int, app_version: str, build) -> None:
        def write(conn):
            conn.execute(
                "INSERT OR REPLACE INTO instances VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?)",
                (
                    instance_id,
                    pid,
                    now_ms,
                    now_ms,
                    app_version,
                    build.commit,
                    build.image_tag,
                    build.source_state,
                    ",".join(build.local_patches) or None,
                ),
            )

        self._transaction(write)

    def mark_stopped(self, instance_id: str, now_ms: int) -> None:
        self._transaction(
            lambda conn: conn.execute(
                "UPDATE instances SET stopped_ms = ?, last_heartbeat_ms = ? WHERE instance_id = ?",
                (now_ms, now_ms, instance_id),
            )
        )

    def write_batch(
        self,
        instance_id: str,
        batch: Batch,
        now_ms: int,
        active: list[tuple[str, str, str, str, int]],
        active_ai: list[tuple] = (),
    ) -> int:
        """Persist one collector batch atomically. Returns persisted request count."""
        request_rows, step_rows = [], []
        req_roll: dict[tuple, list] = {}
        step_roll: dict[tuple, list] = {}
        persisted_by_minute: dict[int, int] = {}
        for r in batch.requests:
            request_rows.append(
                (
                    r.request_id,
                    r.ts_ms,
                    r.duration_ms,
                    instance_id,
                    r.client_id,
                    r.route,
                    r.method,
                    r.service,
                    r.status_code,
                    r.error_code,
                    r.bytes_in,
                    r.bytes_out,
                    r.documents,
                    r.pages,
                    r.ai_provider,
                    r.ai_model,
                    r.ai_calls,
                    r.tokens_in,
                    r.tokens_out,
                    r.steps_truncated,
                )
            )
            minute = minute_of(r.ts_ms)
            persisted_by_minute[minute] = persisted_by_minute.get(minute, 0) + 1
            hist = bucket_index(r.duration_ms)
            tokens_known = r.tokens_in is not None and r.tokens_out is not None
            for resolution in RESOLUTIONS:
                key = (
                    resolution,
                    _bucket(r.ts_ms, resolution),
                    r.client_id or "",
                    r.route,
                    r.method,
                    r.service,
                    r.status_code,
                    r.error_code or "",
                    r.ai_model or "",
                )
                agg = req_roll.get(key)
                if agg is None:
                    agg = req_roll[key] = [
                        0,
                        0.0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        0,
                        [0] * BUCKET_COUNT,
                        r.duration_ms,
                        r.duration_ms,
                    ]
                agg[0] += 1
                agg[1] += r.duration_ms
                agg[2] += r.bytes_in
                agg[3] += r.bytes_out
                agg[4] += r.documents
                agg[5] += r.pages or 0
                agg[6] += 1 if r.pages is not None else 0
                agg[7] += r.ai_calls
                agg[8] += r.tokens_in if tokens_known else 0
                agg[9] += r.tokens_out if tokens_known else 0
                agg[10] += 1 if tokens_known and r.ai_calls else 0
                agg[11][hist] += 1
                agg[12] = min(agg[12], r.duration_ms)
                agg[13] = max(agg[13], r.duration_ms)
            for seq, s in enumerate(r.steps):
                step_rows.append(
                    (
                        r.request_id,
                        r.ts_ms,
                        seq,
                        r.route,
                        s.step,
                        s.outcome,
                        s.duration_ms,
                        s.ollama_total_ms,
                        s.ollama_load_ms,
                        s.ollama_prompt_eval_ms,
                        s.ollama_eval_ms,
                        s.tokens_in,
                        s.tokens_out,
                        1 if s.repair_used else 0,
                        s.ai_model,
                    )
                )
                ollama_known = (
                    s.ollama_load_ms is not None
                    and s.ollama_prompt_eval_ms is not None
                    and s.ollama_eval_ms is not None
                )
                step_tokens_known = s.tokens_in is not None and s.tokens_out is not None
                for resolution in RESOLUTIONS:
                    key = (
                        resolution,
                        _bucket(r.ts_ms, resolution),
                        r.route,
                        s.step,
                        s.outcome,
                        s.ai_model or "",
                    )
                    agg = step_roll.get(key)
                    if agg is None:
                        agg = step_roll[key] = [
                            0,
                            0.0,
                            0,
                            0.0,
                            0.0,
                            0.0,
                            0,
                            0,
                            0,
                            [0] * BUCKET_COUNT,
                            s.duration_ms,
                            s.duration_ms,
                            [0, 0.0, 0, 0, 0.0, 0],  # v3 pair sums
                        ]
                    agg[0] += 1
                    agg[1] += s.duration_ms
                    if ollama_known:
                        agg[2] += 1
                        agg[3] += s.ollama_load_ms
                        agg[4] += s.ollama_prompt_eval_ms
                        agg[5] += s.ollama_eval_ms
                    if step_tokens_known:
                        agg[6] += s.tokens_in
                        agg[7] += s.tokens_out
                        agg[8] += 1
                    agg[9][bucket_index(s.duration_ms)] += 1
                    agg[10] = min(agg[10], s.duration_ms)
                    agg[11] = max(agg[11], s.duration_ms)
                    pairs = agg[12]
                    if s.tokens_out is not None and s.ollama_eval_ms is not None and s.ollama_eval_ms > 0:
                        pairs[0] += s.tokens_out
                        pairs[1] += s.ollama_eval_ms
                        pairs[2] += 1
                    if (
                        s.tokens_in is not None
                        and s.ollama_prompt_eval_ms is not None
                        and s.ollama_prompt_eval_ms > 0
                    ):
                        pairs[3] += s.tokens_in
                        pairs[4] += s.ollama_prompt_eval_ms
                        pairs[5] += 1

        quality: dict[int, QualityCounters] = {m: c for m, c in batch.losses.items()}

        def write(conn):
            if request_rows:
                conn.executemany(
                    "INSERT INTO request_events (request_id, ts_ms, duration_ms, instance_id, client_id, "
                    "route, method, service, status_code, error_code, bytes_in, bytes_out, documents, pages, "
                    "ai_provider, ai_model, ai_calls, tokens_in, tokens_out, steps_truncated) "
                    "VALUES (" + ", ".join("?" * 20) + ")",
                    request_rows,
                )
            if step_rows:
                conn.executemany(
                    "INSERT INTO request_steps (request_id, ts_ms, seq, route, step, outcome, duration_ms, "
                    "ollama_total_ms, ollama_load_ms, ollama_prompt_eval_ms, ollama_eval_ms, tokens_in, "
                    "tokens_out, repair_used, ai_model) VALUES (" + ", ".join("?" * 15) + ")",
                    step_rows,
                )
            conn.executemany(
                _REQ_UPSERT,
                [
                    (
                        *key,
                        a[0],
                        a[1],
                        a[2],
                        a[3],
                        a[4],
                        a[5],
                        a[6],
                        a[7],
                        a[8],
                        a[9],
                        a[10],
                        *a[11],
                        a[12],
                        a[13],
                    )
                    for key, a in req_roll.items()
                ],
            )
            conn.executemany(
                _STEP_UPSERT,
                [
                    (*key, a[0], a[1], a[2], a[3], a[4], a[5], a[6], a[7], a[8], *a[9], *a[12], a[10], a[11])
                    for key, a in step_roll.items()
                ],
            )
            if batch.events:
                conn.executemany(
                    "INSERT INTO events (first_ts_ms, last_ts_ms, severity, code, service, client_id, route, "
                    "error_code, request_id, detail, instance_id, repeat_count) "
                    "VALUES (" + ", ".join("?" * 12) + ")",
                    [
                        (
                            e.first_ts_ms,
                            e.last_ts_ms,
                            e.severity,
                            e.code,
                            e.service,
                            e.client_id,
                            e.route,
                            e.error_code,
                            e.request_id,
                            e.detail,
                            instance_id,
                            e.repeat_count,
                        )
                        for e in batch.events
                    ],
                )
            minutes = set(quality) | set(persisted_by_minute)
            conn.executemany(
                _DQ_UPSERT,
                [
                    (
                        m,
                        instance_id,
                        persisted_by_minute.get(m, 0),
                        *(getattr(quality[m], f) if m in quality else 0 for f in _QUALITY_FIELDS),
                    )
                    for m in minutes
                ],
            )
            conn.execute(
                "UPDATE instances SET last_heartbeat_ms = ? WHERE instance_id = ?", (now_ms, instance_id)
            )
            conn.execute("DELETE FROM live_requests WHERE instance_id = ?", (instance_id,))
            conn.execute("DELETE FROM live_ai_steps WHERE instance_id = ?", (instance_id,))
            conn.executemany(
                "INSERT OR REPLACE INTO live_ai_steps VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(instance_id, *item, now_ms) for item in active_ai],
            )
            conn.executemany(
                "INSERT OR REPLACE INTO live_requests VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    (instance_id, rid, route, method, service, started, now_ms)
                    for rid, route, method, service, started in active
                ],
            )
            return len(request_rows)

        return self._transaction(write)

    def write_samples(self, instance_id: str, ts_ms: int, samples) -> None:
        rows = [(ts_ms, instance_id, s.metric, s.source, s.value, s.status, s.reason) for s in samples]
        rollups = [
            (resolution, _bucket(ts_ms, resolution), s.metric, s.source, 1, s.value, s.value, s.value)
            for s in samples
            if s.status == "ok" and s.value is not None
            for resolution in RESOLUTIONS
        ]

        def write(conn):
            conn.executemany(
                "INSERT INTO system_samples (ts_ms, instance_id, metric, source, value, status, reason) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            conn.executemany(_SYS_UPSERT, rollups)

        self._transaction(write)

    def write_probes(self, ts_ms: int, probes, models) -> None:
        def write(conn):
            conn.executemany(
                "INSERT INTO service_probes (ts_ms, service, status, http_status, latency_ms, version) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(ts_ms, p.service, p.status, p.http_status, p.latency_ms, p.version) for p in probes],
            )
            conn.executemany(
                "INSERT INTO ollama_models (ts_ms, name, installed, loaded, size_bytes, size_vram_bytes, "
                "family, parameter_size, quantization, context_length, expires_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        ts_ms,
                        m.name,
                        int(m.installed),
                        int(m.loaded),
                        m.size_bytes,
                        m.size_vram_bytes,
                        m.family,
                        m.parameter_size,
                        m.quantization,
                        m.context_length,
                        m.expires_ms,
                    )
                    for m in models
                ],
            )

        self._transaction(write)

    def acquire_lease(self, name: str, holder: str, now_ms: int, ttl_ms: int) -> bool:
        """Cross-process lease (e.g. only one worker probes upstreams)."""

        def write(conn):
            conn.execute(
                "INSERT INTO leases (name, holder, expires_ms) VALUES (?, ?, ?) "
                "ON CONFLICT (name) DO UPDATE SET holder = excluded.holder, expires_ms = excluded.expires_ms "
                "WHERE leases.holder = excluded.holder OR leases.expires_ms < ?",
                (name, holder, now_ms + ttl_ms, now_ms),
            )
            row = conn.execute("SELECT holder FROM leases WHERE name = ?", (name,)).fetchone()
            return row is not None and row[0] == holder

        return self._transaction(write)

    # --- retention -------------------------------------------------------------

    def apply_retention(self, now_ms: int) -> dict[str, int]:
        r = self._settings.retention
        conn = self._db()
        deleted: dict[str, int] = {}
        chunked = (
            ("request_events", "ts_ms", now_ms - r.raw_days * DAY_MS),
            ("request_steps", "ts_ms", now_ms - r.raw_days * DAY_MS),
            ("system_samples", "ts_ms", now_ms - r.system_raw_hours * HOUR_MS),
            ("service_probes", "ts_ms", now_ms - r.probe_days * DAY_MS),
            ("ollama_models", "ts_ms", now_ms - r.raw_days * DAY_MS),
            ("events", "last_ts_ms", now_ms - r.event_days * DAY_MS),
        )
        for table, column, cutoff in chunked:
            total = 0
            while True:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} WHERE {column} < ? LIMIT ?)",
                    (cutoff, DELETE_CHUNK),
                )
                total += cur.rowcount
                if cur.rowcount < DELETE_CHUNK:
                    break
            deleted[table] = total
        rollup = "resolution_ms = ? AND bucket_ms < ?"
        minute_cutoff = now_ms - r.minute_rollup_days * DAY_MS
        hour_cutoff = now_ms - r.hour_rollup_days * DAY_MS
        quality_cutoff = now_ms - r.data_quality_days * DAY_MS
        plain = (
            ("rollup_requests", rollup, (MINUTE_MS, minute_cutoff)),
            ("rollup_requests", rollup, (HOUR_MS, hour_cutoff)),
            ("rollup_steps", rollup, (MINUTE_MS, minute_cutoff)),
            ("rollup_steps", rollup, (HOUR_MS, hour_cutoff)),
            ("rollup_system", rollup, (MINUTE_MS, now_ms - r.system_minute_days * DAY_MS)),
            ("rollup_system", rollup, (HOUR_MS, hour_cutoff)),
            ("data_quality", "bucket_ms < ?", (quality_cutoff,)),
            ("instances", "stopped_ms IS NOT NULL AND stopped_ms < ?", (quality_cutoff,)),
            ("live_requests", "snapshot_ms < ?", (now_ms - 10 * MINUTE_MS,)),
            ("live_ai_steps", "snapshot_ms < ?", (now_ms - 10 * MINUTE_MS,)),
            ("leases", "expires_ms < ?", (now_ms - HOUR_MS,)),
        )
        for table, where, params in plain:
            cur = conn.execute(f"DELETE FROM {table} WHERE {where}", params)
            deleted[table] = deleted.get(table, 0) + cur.rowcount
        excess = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] - r.max_event_rows
        if excess > 0:
            cur = conn.execute(
                "DELETE FROM events WHERE id IN (SELECT id FROM events ORDER BY last_ts_ms, id LIMIT ?)",
                (excess,),
            )
            deleted["events"] = deleted.get("events", 0) + cur.rowcount
        # A PRAGMA that returns rows only works while it is stepped: without fetchall()
        # SQLite frees exactly one page (Phase M7 finding: the file never shrank).
        conn.execute("PRAGMA incremental_vacuum(4096)").fetchall()
        return deleted

    def prune_oldest(self, fraction: float = 0.1) -> int:
        """Storage pressure: drop the oldest share of the raw tables early."""
        conn = self._db()
        removed = 0
        for table, column in (
            ("request_events", "ts_ms"),
            ("request_steps", "ts_ms"),
            ("system_samples", "ts_ms"),
            ("ollama_models", "ts_ms"),
            ("service_probes", "ts_ms"),
        ):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            limit = int(count * fraction) + (1 if count else 0)
            if limit:
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE id IN (SELECT id FROM {table} ORDER BY {column}, id LIMIT ?)",
                    (limit,),
                )
                removed += cur.rowcount
        conn.execute("PRAGMA incremental_vacuum(16384)").fetchall()  # stepped, see apply_retention
        return removed

    # --- queries -----------------------------------------------------------------

    def request_stats(
        self,
        start_ms: int,
        end_ms: int,
        now_ms: int,
        *,
        client_id: str | None = None,
        route: str | None = None,
    ) -> dict:
        """Totals and latency percentiles for [start_ms, end_ms).

        Exact (raw rows) while the range lies inside the raw retention,
        otherwise estimated from minute or hour histogram rollups - the
        result says which (`percentile_method`, `resolution_ms`) and carries
        the range's data completeness."""
        conn = self._db()
        r = self._settings.retention
        filters, params = "", []
        if client_id is not None:
            filters += " AND client_id = ?"
            params.append(client_id)
        if route is not None:
            filters += " AND route = ?"
            params.append(route)
        result: dict = {"start_ms": start_ms, "end_ms": end_ms}
        if start_ms >= now_ms - r.raw_days * DAY_MS:
            row = conn.execute(
                "SELECT COUNT(*), SUM(status_code >= 400), SUM(status_code >= 500), AVG(duration_ms), "
                "SUM(bytes_in), SUM(bytes_out), SUM(documents) FROM request_events "
                f"WHERE ts_ms >= ? AND ts_ms < ?{filters}",
                (start_ms, end_ms, *params),
            ).fetchone()
            durations = [
                d
                for (d,) in conn.execute(
                    f"SELECT duration_ms FROM request_events WHERE ts_ms >= ? AND ts_ms < ?{filters} "
                    "ORDER BY duration_ms",
                    (start_ms, end_ms, *params),
                )
            ]
            result.update(percentile_method="exact", resolution_ms=None)
            for name, p in PERCENTILES.items():
                result[f"{name}_ms"] = exact_percentile(durations, p)
        else:
            resolution = MINUTE_MS if start_ms >= now_ms - r.minute_rollup_days * DAY_MS else HOUR_MS
            hist_sum = ", ".join(f"SUM({c})" for c in BUCKET_COLUMNS)
            row_full = conn.execute(
                "SELECT SUM(count), SUM(CASE WHEN status_code >= 400 THEN count ELSE 0 END), "
                "SUM(CASE WHEN status_code >= 500 THEN count ELSE 0 END), SUM(dur_sum) / SUM(count), "
                f"SUM(bytes_in), SUM(bytes_out), SUM(documents), MIN(dur_min), MAX(dur_max), {hist_sum} "
                "FROM rollup_requests WHERE resolution_ms = ? AND bucket_ms >= ? AND bucket_ms < ?"
                f"{filters}",
                (resolution, _bucket(start_ms, resolution), end_ms, *params),
            ).fetchone()
            row = row_full[:7]
            counts = [c or 0 for c in row_full[9:]]
            result.update(percentile_method="histogram", resolution_ms=resolution)
            for name, p in PERCENTILES.items():
                result[f"{name}_ms"] = histogram_percentile(counts, p, row_full[7], row_full[8])
        count = row[0] or 0
        result.update(
            count=count,
            failed=row[1] or 0,
            server_errors=row[2] or 0,
            avg_ms=row[3],
            bytes_in=row[4] or 0,
            bytes_out=row[5] or 0,
            documents=row[6] or 0,
            failure_rate=(row[1] or 0) / count if count else None,
        )
        result["completeness"] = self.completeness(start_ms, end_ms, now_ms)
        return result

    def step_stats(self, start_ms: int, end_ms: int) -> list[dict]:
        """Per-step durations from raw rows (exact) - Ollama-reported parts separately."""
        conn = self._db()
        rows = conn.execute(
            "SELECT step, COUNT(*), SUM(outcome != 'ok'), AVG(duration_ms), AVG(ollama_load_ms), "
            "AVG(ollama_prompt_eval_ms), AVG(ollama_eval_ms), SUM(tokens_in), SUM(tokens_out), "
            "SUM(ollama_eval_ms) FILTER (WHERE tokens_out IS NOT NULL AND ollama_eval_ms > 0) "
            "FROM request_steps WHERE ts_ms >= ? AND ts_ms < ? GROUP BY step ORDER BY step",
            (start_ms, end_ms),
        ).fetchall()
        out = []
        for step, count, failed, avg, load, prompt_eval, ev, tin, tout, eval_ms_known in rows:
            durations = [
                d
                for (d,) in conn.execute(
                    "SELECT duration_ms FROM request_steps WHERE step = ? AND ts_ms >= ? AND ts_ms < ? "
                    "ORDER BY duration_ms",
                    (step, start_ms, end_ms),
                )
            ]
            tout_known = conn.execute(
                "SELECT SUM(tokens_out) FROM request_steps WHERE step = ? AND ts_ms >= ? AND ts_ms < ? "
                "AND tokens_out IS NOT NULL AND ollama_eval_ms > 0",
                (step, start_ms, end_ms),
            ).fetchone()[0]
            out.append(
                {
                    "step": step,
                    "count": count,
                    "failed": failed or 0,
                    "avg_ms": avg,
                    "p95_ms": exact_percentile(durations, 0.95),
                    "ollama_load_avg_ms": load,
                    "ollama_prompt_eval_avg_ms": prompt_eval,
                    "ollama_eval_avg_ms": ev,
                    "tokens_in": tin,
                    "tokens_out": tout,
                    # Derived by DocPipe from Ollama-reported counts and durations.
                    "eval_tokens_per_s": (tout_known / (eval_ms_known / 1000))
                    if tout_known and eval_ms_known
                    else None,
                }
            )
        return out

    def completeness(self, start_ms: int, end_ms: int, now_ms: int) -> dict:
        """Data completeness for [start_ms, end_ms): dropped records and
        collector gaps (minutes inside an instance's lifetime without any
        collector heartbeat). `complete` is False as soon as anything is
        known to be missing."""
        conn = self._db()
        first, last = _bucket(start_ms, MINUTE_MS), end_ms
        sums = conn.execute(
            "SELECT SUM(requests_persisted), SUM(requests_dropped_buffer), SUM(requests_dropped_store), "
            "SUM(events_dropped), SUM(steps_truncated), SUM(flush_failures), SUM(task_failures), "
            "SUM(hook_failures) FROM data_quality WHERE bucket_ms >= ? AND bucket_ms < ?",
            (first, last),
        ).fetchone()
        persisted, dropped_buffer, dropped_store, events_dropped, truncated, flush_f, task_f, hook_f = (
            v or 0 for v in sums
        )
        gaps = 0
        for instance_id, started, last_seen, stopped in conn.execute(
            "SELECT instance_id, started_ms, last_heartbeat_ms, stopped_ms FROM instances "
            "WHERE started_ms < ? AND COALESCE(stopped_ms, ?) >= ?",
            (end_ms, now_ms, start_ms),
        ):
            # Expected: full minutes strictly after the start minute and
            # before the minute of the last sign of life / the range end.
            lo = max(first, _bucket(started, MINUTE_MS) + MINUTE_MS)
            hi = min(_bucket(min(end_ms, now_ms), MINUTE_MS), _bucket(stopped or now_ms, MINUTE_MS))
            if hi <= lo:
                continue
            covered = conn.execute(
                "SELECT COUNT(*) FROM data_quality WHERE instance_id = ? AND bucket_ms >= ? "
                "AND bucket_ms < ? AND heartbeats > 0",
                (instance_id, lo, hi),
            ).fetchone()[0]
            gaps += (hi - lo) // MINUTE_MS - covered
        dropped = dropped_buffer + dropped_store
        return {
            "complete": dropped == 0 and gaps == 0,
            "requests_persisted": persisted,
            "requests_dropped": dropped,
            "requests_dropped_buffer": dropped_buffer,
            "requests_dropped_store": dropped_store,
            "events_dropped": events_dropped,
            "steps_truncated": truncated,
            "collector_gap_minutes": gaps,
            "flush_failures": flush_f,
            "task_failures": task_f,
            "hook_failures": hook_f,
            "recorded_ratio": persisted / (persisted + dropped) if persisted + dropped else None,
        }
