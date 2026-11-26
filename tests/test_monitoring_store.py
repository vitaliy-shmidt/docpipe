"""SQLite monitoring store: schema, rollups, percentiles, retention, limits (Phase M2)."""

from __future__ import annotations

import random
import sqlite3

import pytest

from system.monitoring import store as store_module
from system.monitoring.buildinfo import read_build_info
from system.monitoring.context import StepRecord
from system.monitoring.recorder import Batch, FinishedRequest, QualityCounters
from system.monitoring.settings import MonitoringSettings, RetentionSettings
from system.monitoring.stats import (
    BUCKET_COLUMNS,
    bucket_index,
    bucket_range,
    exact_percentile,
    histogram_percentile,
)
from system.monitoring.store import DAY_MS, HOUR_MS, MINUTE_MS, MonitoringStore, StoreUnavailable

NOW = 1_800_000_000_000 - 1_800_000_000_000 % HOUR_MS  # fixed UTC instant, hour aligned


def make_store(tmp_path, **overrides) -> MonitoringStore:
    store = MonitoringStore(MonitoringSettings(enabled=True, data_dir=tmp_path / "mon", **overrides))
    store.open()
    store.register_instance(
        instance_id="i1", pid=1, now_ms=NOW - 30 * DAY_MS, app_version="0.1.0", build=read_build_info({})
    )
    return store


def request(
    ts: int,
    duration: float,
    *,
    client="c1",
    status=200,
    route="/api/v1/health",
    steps=(),
    tokens=(None, None),
    ai_calls=0,
    request_id=None,
) -> FinishedRequest:
    return FinishedRequest(
        request_id=request_id or f"r{ts}-{duration}-{random.random()}",
        ts_ms=ts,
        duration_ms=duration,
        client_id=client,
        route=route,
        method="GET",
        service="system",
        status_code=status,
        error_code=None if status < 400 else "timeout",
        bytes_in=10,
        bytes_out=20,
        documents=1 if status == 200 else 0,
        pages=None,
        ai_provider=None,
        ai_model=None,
        ai_calls=ai_calls,
        tokens_in=tokens[0],
        tokens_out=tokens[1],
        steps_truncated=0,
        steps=list(steps),
    )


def write(store, requests, now=NOW, losses=None):
    return store.write_batch("i1", Batch(list(requests), [], losses or {}), now, [])


def test_schema_is_versioned_and_migrations_are_idempotent(tmp_path):
    store = make_store(tmp_path)
    assert store.schema_version() == store_module.SCHEMA_VERSION
    store.close()
    store.open()  # reopening an existing database does not migrate again
    assert store.schema_version() == store_module.SCHEMA_VERSION
    tables = {r[0] for r in store._db().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {
        "request_events",
        "request_steps",
        "rollup_requests",
        "rollup_steps",
        "system_samples",
        "service_probes",
        "ollama_models",
        "events",
        "data_quality",
        "instances",
        "leases",
    } <= tables
    assert store._db().execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    store.close()


def test_a_newer_schema_is_not_touched(tmp_path):
    store = make_store(tmp_path)
    store._db().execute("PRAGMA user_version = 99")
    store.close()
    with pytest.raises(StoreUnavailable):
        store.open()
    assert not store.is_open


def test_rollups_match_the_raw_rows_exactly(tmp_path):
    store = make_store(tmp_path)
    rng = random.Random(7)
    rows = [
        request(
            NOW - rng.randrange(0, 3 * HOUR_MS),
            rng.lognormvariate(4, 1.5),
            client=rng.choice(["c1", "c2"]),
            status=rng.choice([200, 200, 200, 404, 504]),
        )
        for _ in range(2000)
    ]
    # Two batches (e.g. two worker processes / a retried batch) hit the same buckets.
    write(store, rows[:1000])
    write(store, rows[1000:])
    conn = store._db()
    for resolution in (MINUTE_MS, HOUR_MS):
        count, dur_sum, failed = conn.execute(
            "SELECT SUM(count), SUM(dur_sum), SUM(CASE WHEN status_code >= 400 THEN count END) "
            "FROM rollup_requests WHERE resolution_ms = ?",
            (resolution,),
        ).fetchone()
        assert count == 2000
        assert dur_sum == pytest.approx(sum(r.duration_ms for r in rows))
        assert failed == sum(1 for r in rows if r.status_code >= 400)
        hist = conn.execute(
            f"SELECT {', '.join(f'SUM({c})' for c in BUCKET_COLUMNS)} FROM rollup_requests "
            "WHERE resolution_ms = ?",
            (resolution,),
        ).fetchone()
        assert sum(hist) == 2000
    per_client = dict(
        conn.execute(
            "SELECT client_id, SUM(count) FROM rollup_requests WHERE resolution_ms = ? GROUP BY client_id",
            (HOUR_MS,),
        ).fetchall()
    )
    assert per_client == {c: sum(1 for r in rows if r.client_id == c) for c in ("c1", "c2")}
    quality = conn.execute("SELECT SUM(requests_persisted) FROM data_quality").fetchone()[0]
    assert quality == 2000
    store.close()


def test_exact_percentiles_use_nearest_rank():
    values = sorted(range(1, 101))
    assert exact_percentile(values, 0.5) == 50
    assert exact_percentile(values, 0.95) == 95
    assert exact_percentile(values, 0.99) == 99
    assert exact_percentile([], 0.5) is None
    assert exact_percentile([7.0], 0.99) == 7.0


def test_histogram_estimate_stays_inside_the_exact_values_bucket():
    rng = random.Random(1)
    for _ in range(50):
        values = sorted(rng.lognormvariate(rng.uniform(1, 9), rng.uniform(0.2, 2)) for _ in range(500))
        counts = [0] * len(BUCKET_COLUMNS)
        for v in values:
            counts[bucket_index(v)] += 1
        for p in (0.5, 0.95, 0.99):
            exact = exact_percentile(values, p)
            estimate = histogram_percentile(counts, p, values[0], values[-1])
            lower, upper = bucket_range(bucket_index(exact))
            assert max(lower, values[0]) <= estimate <= min(upper, values[-1])


def test_request_stats_are_exact_inside_raw_retention_and_estimated_beyond(tmp_path):
    store = make_store(tmp_path)
    recent = [request(NOW - MINUTE_MS * 5, d) for d in (10, 20, 30, 40, 1000)]
    older = [request(NOW - 10 * DAY_MS, d) for d in (10, 20, 30, 40, 1000)]
    ancient = [request(NOW - 20 * DAY_MS, d) for d in (10, 20, 30, 40, 1000)]
    write(store, recent + older + ancient)
    exact = store.request_stats(NOW - HOUR_MS, NOW, NOW)
    assert (exact["count"], exact["percentile_method"], exact["p50_ms"], exact["p95_ms"]) == (
        5,
        "exact",
        30.0,
        1000.0,
    )
    assert exact["avg_ms"] == pytest.approx(220.0)
    minute = store.request_stats(NOW - 11 * DAY_MS, NOW - 9 * DAY_MS, NOW)
    assert (minute["count"], minute["percentile_method"], minute["resolution_ms"]) == (
        5,
        "histogram",
        MINUTE_MS,
    )
    assert 20 <= minute["p50_ms"] <= 50
    hour = store.request_stats(NOW - 21 * DAY_MS, NOW - 19 * DAY_MS, NOW)
    assert (hour["count"], hour["resolution_ms"]) == (5, HOUR_MS)
    by_client = store.request_stats(NOW - HOUR_MS, NOW, NOW, client_id="nobody")
    assert by_client["count"] == 0 and by_client["p95_ms"] is None and by_client["failure_rate"] is None
    store.close()


def test_step_stats_separate_docpipe_and_ollama_timings(tmp_path):
    store = make_store(tmp_path)
    step = StepRecord(
        step="ai.generate",
        outcome="ok",
        duration_ms=2000.0,
        ollama_total_ms=1900.0,
        ollama_load_ms=500.0,
        ollama_prompt_eval_ms=400.0,
        ollama_eval_ms=1000.0,
        tokens_in=300,
        tokens_out=50,
        ai_model="m",
    )
    write(store, [request(NOW - MINUTE_MS, 2100, steps=[step], ai_calls=1, tokens=(300, 50))])
    [stats] = store.step_stats(NOW - HOUR_MS, NOW)
    assert stats["step"] == "ai.generate" and stats["count"] == 1 and stats["avg_ms"] == 2000.0
    assert stats["ollama_load_avg_ms"] == 500.0 and stats["eval_tokens_per_s"] == pytest.approx(50.0)
    store.close()


def test_retention_deletes_per_table_and_keeps_recent_data(tmp_path):
    store = make_store(
        tmp_path,
        retention=RetentionSettings(
            raw_days=1, minute_rollup_days=2, hour_rollup_days=3, max_event_rows=1000
        ),
    )
    write(
        store, [request(NOW - 5 * DAY_MS, 5), request(NOW - int(1.5 * DAY_MS), 5), request(NOW - HOUR_MS, 5)]
    )
    deleted = store.apply_retention(NOW)
    conn = store._db()
    assert conn.execute("SELECT COUNT(*) FROM request_events").fetchone()[0] == 1
    minute_buckets = conn.execute(
        "SELECT COUNT(DISTINCT bucket_ms) FROM rollup_requests WHERE resolution_ms = ?", (MINUTE_MS,)
    ).fetchone()[0]
    hour_buckets = conn.execute(
        "SELECT COUNT(DISTINCT bucket_ms) FROM rollup_requests WHERE resolution_ms = ?", (HOUR_MS,)
    ).fetchone()[0]
    assert (minute_buckets, hour_buckets) == (2, 2)
    assert deleted["request_events"] == 2
    store.close()


def test_event_rows_are_capped(tmp_path):
    from system.monitoring.recorder import EventRecord

    store = make_store(tmp_path, retention=RetentionSettings(max_event_rows=1000))
    events = [
        EventRecord(
            NOW - i, NOW - i, "INFO", "monitoring_started", "monitoring", None, None, None, None, None
        )
        for i in range(1500)
    ]
    store.write_batch("i1", Batch([], events, {}), NOW, [])
    store.apply_retention(NOW)
    assert store._db().execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1000
    # The newest rows are kept.
    assert store._db().execute("SELECT MIN(last_ts_ms) FROM events").fetchone()[0] == NOW - 999
    store.close()


def test_locked_database_fails_the_write_without_losing_the_batch(tmp_path, monkeypatch):
    monkeypatch.setattr(store_module, "BUSY_TIMEOUT_SECONDS", 0.2)
    store = make_store(tmp_path)
    blocker = sqlite3.connect(str(store.path), timeout=0.1, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    with pytest.raises(sqlite3.OperationalError):
        write(store, [request(NOW, 1.0)])
    blocker.execute("ROLLBACK")
    blocker.close()
    assert write(store, [request(NOW, 1.0)]) == 1  # the same batch succeeds afterwards
    store.close()


def test_size_limit_is_enforced_by_sqlite_itself(tmp_path):
    from system.monitoring.settings import StorageSettings

    store = make_store(tmp_path, storage=StorageSettings(max_bytes=1024 * 1024, min_free_disk_bytes=0))
    with pytest.raises(sqlite3.OperationalError, match="full"):
        for i in range(200):
            write(store, [request(NOW - i * MINUTE_MS - j, 1.0) for j in range(500)])
    assert store.file_bytes() < 8 * 1024 * 1024  # db + wal + shm stay bounded
    store.close()


def test_file_bytes_counts_wal_and_shm(tmp_path):
    store = make_store(tmp_path)
    write(store, [request(NOW, 1.0) for _ in range(100)])
    parts = sum(
        (store.path.parent / (store.path.name + s)).stat().st_size
        for s in ("", "-wal", "-shm")
        if (store.path.parent / (store.path.name + s)).exists()
    )
    assert store.file_bytes() == parts and (store.path.parent / (store.path.name + "-wal")).exists()
    store.close()


def test_prune_oldest_removes_the_oldest_raw_rows(tmp_path):
    store = make_store(tmp_path)
    write(store, [request(NOW - i * 1000, 1.0) for i in range(100)])
    removed = store.prune_oldest(0.1)
    oldest = store._db().execute("SELECT MIN(ts_ms) FROM request_events").fetchone()[0]
    assert removed >= 10 and oldest > NOW - 100 * 1000
    store.close()


def test_lease_has_a_single_holder_until_it_expires(tmp_path):
    store = make_store(tmp_path)
    assert store.acquire_lease("probes", "a", NOW, 1000)
    assert not store.acquire_lease("probes", "b", NOW + 500, 1000)
    assert store.acquire_lease("probes", "a", NOW + 600, 1000)  # renewal by the holder
    assert store.acquire_lease("probes", "b", NOW + 2000, 1000)  # after expiry
    store.close()


def test_completeness_reports_drops_and_collector_gaps(tmp_path):
    store = make_store(tmp_path)
    start = NOW - 10 * MINUTE_MS
    store._db().execute("UPDATE instances SET started_ms = ? WHERE instance_id = 'i1'", (start - MINUTE_MS,))
    losses = {start + m * MINUTE_MS: QualityCounters(heartbeats=1) for m in range(10) if m not in (3, 4)}
    losses[start] = QualityCounters(heartbeats=1, requests_dropped_buffer=2)
    write(store, [request(start + 1, 1.0)], losses=losses)
    result = store.completeness(start, NOW, NOW)
    assert result["complete"] is False
    assert result["collector_gap_minutes"] == 2
    assert result["requests_dropped"] == 2 and result["requests_persisted"] == 1
    assert result["recorded_ratio"] == pytest.approx(1 / 3)
    full = {start + m * MINUTE_MS: QualityCounters(heartbeats=1) for m in range(10)}
    write(store, [], losses=full)
    assert store.completeness(start + MINUTE_MS, NOW, NOW)["collector_gap_minutes"] == 0
    store.close()


def test_database_file_permissions_are_restrictive(tmp_path):
    import os
    import stat

    store = make_store(tmp_path)
    if os.name == "posix":
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(store.path.parent).st_mode) == 0o700
    store.close()
