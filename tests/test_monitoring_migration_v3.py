"""Schema migration v3 (Phase M5): paired throughput sums and live AI step snapshots.

A pre-v3 database is produced from a current one by removing exactly what v3
adds (the six pair columns and live_ai_steps) and resetting user_version - the
remaining tables are then byte-for-byte what the v1/v2 writer stored.
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from system.monitoring import store as store_module
from system.monitoring.context import StepRecord
from system.monitoring.settings import MonitoringSettings
from system.monitoring.store import HOUR_MS, MINUTE_MS, MonitoringStore
from tests.test_monitoring_store import NOW, make_store, request, write

PAIRS = store_module._PAIR_COLUMNS
V2_INDEXES = [s.split(" IF NOT EXISTS ")[1].split(" ON ")[0] for s in store_module._MIGRATION_2]


def ai_request(ts, tokens_out, eval_ms, tokens_in=1000, prompt_ms=200.0, outcome="ok", model="m1"):
    step = StepRecord(
        step="ai.generate",
        outcome=outcome,
        duration_ms=900.0,
        ollama_total_ms=880.0,
        ollama_load_ms=0.0,
        ollama_prompt_eval_ms=prompt_ms,
        ollama_eval_ms=eval_ms,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        ai_model=model,
    )
    return request(ts, 950.0, route="/api/v1/documents/analyze", steps=[step], ai_calls=1)


def seed_ai(store):
    rows = [
        ai_request(NOW - 3 * HOUR_MS + 1_000, 100, 1000.0),
        ai_request(NOW - 3 * HOUR_MS + 2_000, 300, 1000.0),
        ai_request(NOW - 3 * HOUR_MS + 3_000, 50, None),  # older Ollama: no eval duration
        ai_request(
            NOW - 3 * HOUR_MS + 4_000, None, None, tokens_in=None, prompt_ms=None, outcome="ai_timeout"
        ),
        ai_request(NOW - 2 * HOUR_MS + 1_000, 200, 500.0, tokens_in=None),  # prompt tokens unknown
        ai_request(NOW - 2 * HOUR_MS + 2_000, 80, 0.0),  # zero duration is never a pair
        request(NOW - 2 * HOUR_MS + 3_000, 12.0),  # non-AI request
    ]
    write(store, rows, now=NOW)
    return rows


def step_rollups(conn):
    return {
        r[:6]: r[6:]
        for r in conn.execute(
            "SELECT resolution_ms, bucket_ms, route, step, outcome, ai_model, count, dur_sum, "
            + ", ".join(PAIRS)
            + " FROM rollup_steps ORDER BY 1, 2, 3, 4, 5, 6"
        )
    }


def snapshot(conn, table):
    return sorted(conn.execute(f"SELECT * FROM {table}").fetchall(), key=repr)


def downgrade(path, to_version=2):
    conn = sqlite3.connect(str(path), isolation_level=None)
    for column in PAIRS:
        conn.execute(f"ALTER TABLE rollup_steps DROP COLUMN {column}")
    conn.execute("DROP TABLE live_ai_steps")
    if to_version == 1:
        for index in V2_INDEXES:
            conn.execute(f"DROP INDEX {index}")
    conn.execute(f"PRAGMA user_version = {to_version}")
    conn.close()


def reopen(tmp_path):
    store = MonitoringStore(MonitoringSettings(enabled=True, data_dir=tmp_path / "mon"))
    store.open()
    return store


@pytest.mark.parametrize("from_version", [1, 2])
def test_migration_preserves_data_and_reconstructs_pair_sums(tmp_path, from_version):
    store = make_store(tmp_path)
    seed_ai(store)
    conn = store._db()
    expected = step_rollups(conn)  # what the v3 writer produced online
    before = {
        t: snapshot(conn, t) for t in ("request_events", "request_steps", "rollup_requests", "data_quality")
    }
    path = store.path
    store.close()
    downgrade(path, from_version)

    store = reopen(tmp_path)
    conn = store._db()
    assert store.schema_version() == 3
    assert {t: snapshot(conn, t) for t in before} == before  # v1/v2 data untouched
    assert step_rollups(conn) == expected  # complete buckets reconstructed exactly
    hour = expected[(HOUR_MS, NOW - 3 * HOUR_MS, "/api/v1/documents/analyze", "ai.generate", "ok", "m1")]
    # count, dur_sum, eval tokens/ms/pairs, prompt tokens/ms/pairs
    assert hour == (3, 2700.0, 400, 2000.0, 2, 3000, 600.0, 3)
    indexes = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    assert set(V2_INDEXES) <= indexes
    assert conn.execute("SELECT COUNT(*) FROM live_ai_steps").fetchone()[0] == 0
    store.close()


def test_incomplete_buckets_stay_not_available_and_are_never_double_counted(tmp_path):
    store = make_store(tmp_path)
    seed_ai(store)
    conn = store._db()
    path = store.path
    # retention removed one raw row of the first hour: that bucket is no longer complete
    conn.execute("DELETE FROM request_steps WHERE ts_ms = ?", (NOW - 3 * HOUR_MS + 2_000,))
    store.close()
    downgrade(path)
    store = reopen(tmp_path)
    conn = store._db()
    rows = step_rollups(conn)
    key = (HOUR_MS, NOW - 3 * HOUR_MS, "/api/v1/documents/analyze", "ai.generate", "ok", "m1")
    assert rows[key][2:] == (None,) * 6  # not available instead of a partial sum
    minute = (MINUTE_MS, NOW - 3 * HOUR_MS, "/api/v1/documents/analyze", "ai.generate", "ok", "m1")
    assert rows[minute][2:] == (None,) * 6  # the deleted row was in this minute
    later = (HOUR_MS, NOW - 2 * HOUR_MS, "/api/v1/documents/analyze", "ai.generate", "ok", "m1")
    assert rows[later][2:] == (200, 500.0, 1, 1000, 200.0, 1)  # complete; 0 ms eval is no pair

    # new writes after the migration: a NULL bucket stays NULL, a filled one adds exactly once
    write(store, [ai_request(NOW - 3 * HOUR_MS + 30 * MINUTE_MS, 10, 100.0)], now=NOW)
    write(store, [ai_request(NOW - 2 * HOUR_MS + 30 * MINUTE_MS, 10, 100.0)], now=NOW)
    rows = step_rollups(conn)
    assert rows[key][0] == 4 and rows[key][2:] == (None,) * 6
    assert rows[later][0] == 3 and rows[later][2:] == (210, 600.0, 2, 2000, 400.0, 2)
    store.close()


def test_backfill_is_bounded(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    seed_ai(store)
    path = store.path
    store.close()
    downgrade(path)
    monkeypatch.setattr(store_module, "BACKFILL_MAX_RAW_ROWS", 3)
    store = reopen(tmp_path)
    rows = step_rollups(store._db())
    assert store.schema_version() == 3
    assert all(v[2:] == (None,) * 6 for v in rows.values())  # too many raw rows: no reconstruction
    store.close()


def test_concurrent_workers_migrate_once(tmp_path):
    store = make_store(tmp_path)
    seed_ai(store)
    expected = step_rollups(store._db())
    path = store.path
    store.close()
    downgrade(path)
    barrier, errors, stores = threading.Barrier(4), [], []

    def worker():
        try:
            barrier.wait(5)
            s = reopen(tmp_path)
            stores.append(s)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(30)
    assert not errors and len(stores) == 4 and all(s.is_open for s in stores)
    assert all(s.schema_version() == 3 for s in stores)
    assert step_rollups(stores[0]._db()) == expected
    for s in stores:
        s.close()


def test_auth_database_is_not_touched(tmp_path):
    auth = tmp_path / "mon" / "monitor-auth.db"
    store = make_store(tmp_path)
    store.close()
    downgrade(store.path)
    assert not auth.exists()
    reopen(tmp_path).close()
    assert not auth.exists()  # the telemetry migration never creates or opens the auth database
