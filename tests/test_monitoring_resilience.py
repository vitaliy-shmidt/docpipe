"""Monitoring failures never break DocPipe, and losses stay visible (Phase M2)."""

from __future__ import annotations

import shutil
import threading
import time
from collections import namedtuple

import pytest

from system.monitoring import monitor as monitor_module
from system.monitoring.monitor import THREAD_NAME, Monitor
from system.monitoring.recorder import FinishedRequest, Recorder
from system.monitoring.settings import StorageSettings
from tests.conftest import VALID_KEY, auth_headers, pdf_bytes
from tests.monitoring_support import build_settings, db_rows, monitored_client

Usage = namedtuple("Usage", "total used free")


class Clock:
    def __init__(self, start: int = 1_800_000_000_000) -> None:
        self.now = start

    def __call__(self) -> int:
        return self.now


def finished(ts: int, request_id: str) -> FinishedRequest:
    return FinishedRequest(
        request_id=request_id,
        ts_ms=ts,
        duration_ms=1.0,
        client_id=None,
        route="/x",
        method="GET",
        service="system",
        status_code=200,
        error_code=None,
        bytes_in=0,
        bytes_out=0,
        documents=0,
        pages=None,
        ai_provider=None,
        ai_model=None,
        ai_calls=0,
        tokens_in=None,
        tokens_out=None,
        steps_truncated=0,
    )


def make_monitor(tmp_path, clock=None, **overrides) -> Monitor:
    settings = build_settings(tmp_path, **overrides)
    return Monitor(settings, clock=clock or Clock(), env={})


def wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_recorder_is_thread_safe_under_contention():
    recorder = Recorder(max_requests=100_000, max_events=100)

    def work(n):
        for i in range(2000):
            recorder.begin(f"{n}-{i}", "/x", "GET", "system", 0)
            recorder.finish(finished(0, f"{n}-{i}"))

    threads = [threading.Thread(target=work, args=(n,)) for n in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    batch = recorder.take()
    assert len(batch.requests) == 100_000 and len({r.request_id for r in batch.requests}) == 100_000
    assert recorder.pending()["active_requests"] == 0


def test_buffer_overflow_drops_and_counts_and_reports_incomplete_data(tmp_path):
    clock = Clock()
    mon = make_monitor(tmp_path, clock, buffer_max_requests=5)
    mon.store.open()
    mon.store.register_instance(
        instance_id=mon.instance_id, pid=1, now_ms=clock.now - 120_000, app_version="t", build=mon.build
    )
    for i in range(8):
        mon.recorder.finish(finished(clock.now, f"r{i}"))
    assert mon.recorder.totals["requests_dropped_buffer"] == 3
    assert mon.flush_now() == 5
    stats = mon.store.request_stats(clock.now - 60_000, clock.now + 60_000, clock.now + 1)
    assert stats["count"] == 5
    c = stats["completeness"]
    assert c["complete"] is False and c["requests_dropped_buffer"] == 3
    assert c["recorded_ratio"] == pytest.approx(5 / 8)
    codes = [r[0] for r in db_rows(mon.store.path, "SELECT code FROM events")]
    assert codes.count("buffer_overflow") == 1  # reported once per minute, not per request
    mon.store.close()


def test_unopenable_store_never_affects_requests(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    (tmp_path / "mon").write_text("a file where the data directory should be", encoding="utf-8")
    with monitored_client(monkeypatch, tmp_path, buffer_max_requests=100) as client:
        for _ in range(150):
            response = client.post(
                "/api/v1/documents/extract-text",
                headers=auth_headers(VALID_KEY),
                files={"file": ("x.pdf", pdf_bytes(), "application/pdf")},
            )
            assert response.status_code == 200
        assert client.monitor.flush_now() == 0
        health = client.monitor.health()
    assert health["store"] == "unavailable"
    # Bounded memory: what does not fit is dropped and counted, nothing is lost silently.
    assert health["requests_buffered"] == 100
    assert health["totals"]["requests_dropped_buffer"] == 50
    assert health["unsaved_requests_dropped"] == 50


def test_store_recovers_and_persists_buffered_data(tmp_path, monkeypatch):
    clock = Clock()
    mon = make_monitor(tmp_path, clock)
    mon.store.open()
    mon.store.register_instance(
        instance_id=mon.instance_id, pid=1, now_ms=clock.now, app_version="t", build=mon.build
    )
    original = mon.store.write_batch

    def failing(*args, **kwargs):
        raise OSError("disk I/O error")

    monkeypatch.setattr(mon.store, "write_batch", failing)
    for i in range(10):
        mon.recorder.finish(finished(clock.now, f"r{i}"))
    assert mon.flush_now() == 0 and mon.status["store"] == "write_failing"
    monkeypatch.setattr(mon.store, "write_batch", original)
    assert mon.flush_now() == 10
    c = mon.store.completeness(clock.now - 60_000, clock.now + 60_000, clock.now + 1)
    assert c["requests_persisted"] == 10 and c["requests_dropped"] == 0
    assert c["flush_failures"] == 1 and c["complete"] is True  # retried, nothing lost
    mon.flush_now()  # the recovery event itself is written with the next batch
    codes = [r[0] for r in db_rows(mon.store.path, "SELECT code FROM events ORDER BY id")]
    assert "store_unavailable" in codes and "store_recovered" in codes
    mon.store.close()


def test_low_disk_space_pauses_writes(tmp_path, monkeypatch):
    clock = Clock()
    mon = make_monitor(tmp_path, clock, storage=StorageSettings(min_free_disk_bytes=10 * 1024 * 1024))
    mon.store.open()
    mon.recorder.finish(finished(clock.now, "r1"))
    real_disk_usage = shutil.disk_usage
    monkeypatch.setattr(monitor_module.shutil, "disk_usage", lambda path: Usage(100, 99, 1024))
    assert mon.flush_now() == 0 and mon.health()["writes_blocked"] == "disk"
    monkeypatch.setattr(
        monitor_module.shutil,
        "disk_usage",
        lambda path: Usage(2**40, 0, 2**40) if path else real_disk_usage(path),
    )
    assert mon.flush_now() == 1 and mon.health()["writes_blocked"] is None
    codes = [r[0] for r in db_rows(mon.store.path, "SELECT code FROM events")]
    assert "disk_space_low" in codes
    mon.store.close()


def test_size_limit_including_wal_pauses_writes(tmp_path, monkeypatch):
    clock = Clock()
    mon = make_monitor(tmp_path, clock)
    mon.store.open()
    mon.recorder.finish(finished(clock.now, "r1"))
    monkeypatch.setattr(mon.store, "file_bytes", lambda: mon.settings.storage.max_bytes)
    assert mon.flush_now() == 0 and mon.health()["writes_blocked"] == "size"
    assert mon.health()["requests_buffered"] == 1
    mon.store.close()


def test_disk_full_error_from_sqlite_is_contained(tmp_path):
    clock = Clock()
    mon = make_monitor(
        tmp_path,
        clock,
        buffer_max_requests=1000,
        storage=StorageSettings(max_bytes=1024 * 1024, min_free_disk_bytes=0),
    )
    mon.store.open()
    persisted = 0
    for round_ in range(60):
        for i in range(1000):
            mon.recorder.finish(finished(clock.now + round_, f"{round_}-{i}"))
        persisted += mon.flush_now()
    health = mon.health()
    assert health["store"] in ("write_failing", "open")
    assert persisted < 60_000
    lost = health["totals"]["requests_dropped_buffer"] + health["totals"]["requests_dropped_store"]
    assert persisted + lost + health["requests_buffered"] == 60_000  # every request accounted for
    mon.store.close()


def test_failing_collector_task_is_isolated(tmp_path):
    mon = make_monitor(tmp_path, flush_interval_seconds=0.05, system_sample_interval_seconds=0.05)

    class BrokenSampler:
        def sample(self, mono):
            raise RuntimeError("sampler bug")

    mon.sampler = BrokenSampler()
    mon.start()
    try:
        assert wait_for(lambda: mon.recorder.totals["requests_recorded"] == 0 and mon.status["last_flush_ms"])
        assert wait_for(
            lambda: (
                db_rows(mon.store.path, "SELECT COUNT(*) FROM events WHERE code = 'collector_task_failed'")[
                    0
                ][0]
                > 0
            )
        )
        assert mon.health()["collector"] == "running"
        detail = db_rows(mon.store.path, "SELECT detail FROM events WHERE code = 'collector_task_failed'")
        assert detail[0][0] == "sample"
    finally:
        mon.stop()


def test_dead_collector_thread_is_reported(tmp_path, monkeypatch):
    mon = make_monitor(tmp_path)
    monkeypatch.setattr(mon, "_run", lambda: None)  # thread ends immediately
    mon.start()
    assert wait_for(lambda: not mon._thread.is_alive())
    assert mon.health()["collector"] == "dead"
    mon.stop()


def test_stop_joins_the_thread_and_persists_the_final_batch(tmp_path):
    mon = make_monitor(tmp_path)
    mon.start()
    mon.recorder.finish(finished(mon._clock(), "last"))
    mon.stop()
    assert not any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())
    assert db_rows(mon.store.path, "SELECT request_id FROM request_events") == [("last",)]
    stopped = db_rows(mon.store.path, "SELECT stopped_ms FROM instances")
    assert stopped[0][0] is not None


def test_collector_heartbeats_cover_every_minute(tmp_path):
    clock = Clock(1_800_000_000_000 - 1_800_000_000_000 % 60_000)
    mon = make_monitor(tmp_path, clock)
    mon.store.open()
    mon.store.register_instance(
        instance_id=mon.instance_id, pid=1, now_ms=clock.now, app_version="t", build=mon.build
    )
    for _ in range(10 * 12):  # 10 minutes of 5 s flushes
        clock.now += 5_000
        mon.flush_now()
    c = mon.store.completeness(clock.now - 9 * 60_000, clock.now, clock.now)
    assert c["collector_gap_minutes"] == 0 and c["complete"] is True
    clock.now += 5 * 60_000  # collector stalled for five minutes
    mon.flush_now()
    c = mon.store.completeness(clock.now - 9 * 60_000, clock.now, clock.now)
    assert c["collector_gap_minutes"] >= 4 and c["complete"] is False
    mon.store.close()


def test_repeated_write_failures_reopen_the_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor_module, "STORE_RETRY_SECONDS", 0.0)
    clock = Clock()
    mon = make_monitor(tmp_path, clock)
    mon._open_store()
    original = type(mon.store).write_batch

    def failing(self, *args, **kwargs):
        raise OSError("broken connection")

    monkeypatch.setattr(type(mon.store), "write_batch", failing)
    mon.recorder.finish(finished(clock.now, "r1"))
    for _ in range(monitor_module.MAX_CONSECUTIVE_WRITE_FAILURES):
        assert mon.flush_now() == 0
    assert not mon.store.is_open  # closed for a clean reopen
    monkeypatch.setattr(type(mon.store), "write_batch", original)
    assert mon.flush_now() == 1 and mon.store.is_open and mon.status["store"] == "open"
    mon.store.close()


def test_maintenance_applies_retention_and_storage_pressure(tmp_path, monkeypatch):
    clock = Clock()
    mon = make_monitor(tmp_path, clock)
    mon._open_store()
    for i in range(50):
        mon.recorder.finish(finished(clock.now - i, f"r{i}"))
    mon.flush_now()
    # Above the pressure threshold (90 %), below the hard limit.
    monkeypatch.setattr(mon.store, "file_bytes", lambda: int(mon.settings.storage.max_bytes * 0.95))
    mon.maintenance_now()
    mon.flush_now()
    remaining = db_rows(mon.store.path, "SELECT COUNT(*) FROM request_events")[0][0]
    codes = [r[0] for r in db_rows(mon.store.path, "SELECT code FROM events")]
    mon.store.close()
    assert remaining < 50 and "storage_pressure" in codes


def test_storage_limit_is_left_again_after_real_pruning(tmp_path):
    """Phase M7 load test finding: pruning deleted rows but the file never shrank
    (PRAGMA incremental_vacuum was not stepped), so a store that once reached
    storage.max_bytes blocked all telemetry writes for good. Checked here with real
    file sizes, no mocks: above the limit writes pause visibly, maintenance prunes,
    the file shrinks below the limit and writes resume."""
    from system.monitoring.recorder import Batch
    from system.monitoring.settings import MIB

    clock = Clock()
    limit = 16 * MIB
    # The store caps its own page count at max_bytes, so the realistic way above the
    # limit is a limit lowered later: fill with the default limit, then reopen.
    filler = make_monitor(tmp_path, clock)
    filler._open_store()
    i = 0
    while filler.store.file_bytes() < limit * 1.1:
        batch = [finished(clock.now - 3_600_000 - n, f"fill-{i + n}") for n in range(4000)]
        i += len(batch)
        filler.store.write_batch(filler.instance_id, Batch(batch, [], {}), clock.now, [])
        filler.store.checkpoint()
    filler.store.close()
    mon = make_monitor(tmp_path, clock, storage=StorageSettings(max_bytes=limit, min_free_disk_bytes=0))
    mon._open_store()
    assert mon.store.file_bytes() >= limit
    mon.recorder.finish(finished(clock.now, "blocked"))
    assert mon.flush_now() == 0 and mon._writes_blocked == "size"  # visible pause, nothing written
    for _ in range(10):
        mon.maintenance_now()
        if mon.store.file_bytes() < limit * monitor_module.PRESSURE_RATIO:
            break
    assert mon.store.file_bytes() < limit  # the file really shrank
    mon.recorder.finish(finished(clock.now, "after"))
    assert mon.flush_now() >= 1 and mon._writes_blocked is None  # writes resume
    codes = {r[0] for r in db_rows(mon.store.path, "SELECT code FROM events")}
    mon.store.close()
    assert {"storage_limit_reached", "storage_pressure"} <= codes
