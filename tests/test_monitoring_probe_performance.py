"""Collector behaviour with slow or unreachable upstreams (Phase M5).

Real local sockets (not a mock transport), because the finding is about
socket timeouts: httpx applies `timeout` per phase, so an upstream that sends
one byte just before every read timeout could hold a probe - and with it the
collector thread that also flushes - for as long as it keeps trickling. Probes
now have a total deadline per request. Synthetic local servers only.
"""

from __future__ import annotations

import dataclasses
import socket
import threading
import time
from contextlib import contextmanager

import pytest

from system.monitoring.probes import ServiceProber
from system.monitoring.settings import ProbeSettings
from tests.conftest import VALID_KEY, auth_headers
from tests.monitoring_support import build_settings, db_rows, monitored_client

TIMEOUT = 0.5
SLACK = 0.6  # scheduling / connection set-up margin for slow CI machines


@contextmanager
def upstream(mode: str):
    """'trickle': 200 OK, then one byte every 0.1 s; 'hang': accepts, never answers."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(32)
    stop = threading.Event()

    def handle(conn):
        try:
            conn.recv(4096)
            if mode == "trickle":
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 100000\r\n\r\n"
                )
                while not stop.wait(0.1):
                    conn.sendall(b" ")
            else:
                stop.wait(30)
        except OSError:
            pass
        finally:
            conn.close()

    def loop():
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{srv.getsockname()[1]}"
    finally:
        stop.set()
        srv.close()


def closed_port_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("mode", ["trickle", "hang", "refused"])
def test_each_probe_request_has_a_total_deadline(mode):
    with upstream("hang" if mode == "refused" else mode) as url:
        url = closed_port_url() if mode == "refused" else url
        prober = ServiceProber(
            ollama_url=url, stirling_url=url, probe_ollama=True, probe_stirling=True, timeout_seconds=TIMEOUT
        )
        try:
            started = time.monotonic()
            stirling = prober.probe_stirling()
            middle = time.monotonic()
            ollama, models = prober.probe_ollama()
            ended = time.monotonic()
        finally:
            prober.close()
    assert stirling.status in ("timeout", "connection_error") and ollama.status in (
        "timeout",
        "connection_error",
    )
    assert models == []
    # before the fix a trickling upstream held each probe for as long as it kept sending
    assert middle - started < TIMEOUT + SLACK
    assert ended - middle < TIMEOUT + SLACK  # the first failing Ollama request ends the probe


def _settings(tmp_path, url):
    settings = build_settings(
        tmp_path,
        flush_interval_seconds=1,
        probe_interval_seconds=10,
        probes=ProbeSettings(ollama=True, stirling=True, timeout_seconds=TIMEOUT),
    )
    return dataclasses.replace(
        settings,
        ollama=dataclasses.replace(settings.ollama, base_url=url),
        stirling=dataclasses.replace(settings.stirling, base_url=url),
    )


def test_slow_probes_neither_block_requests_nor_lose_telemetry(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with (
        upstream("trickle") as url,
        monitored_client(monkeypatch, tmp_path, settings=_settings(tmp_path, url)) as client,
    ):
        monitor = client.monitor
        probe = threading.Thread(target=monitor.probe_now)
        probe.start()
        time.sleep(0.05)
        latencies = []
        for _ in range(100):  # API traffic while a probe is stuck on the slow upstream
            t0 = time.monotonic()
            assert client.get("/api/v1/health", headers=auth_headers(VALID_KEY)).status_code == 200
            latencies.append(time.monotonic() - t0)
        t0 = time.monotonic()
        persisted = monitor.flush_now()  # the store lock is not held during probe HTTP
        flush_s = time.monotonic() - t0
        probe.join(10)
        assert not probe.is_alive()
        assert max(latencies) < 0.5 and flush_s < 0.5
        assert persisted >= 0 and monitor.recorder.totals["requests_dropped_buffer"] == 0
        monitor.flush_now()
        stored = db_rows(
            monitor.store.path, "SELECT COUNT(*) FROM request_events WHERE route = '/api/v1/health'"
        )
        assert stored[0][0] >= 100  # nothing lost while the probe was stuck
        statuses = {
            r[0]: r[1] for r in db_rows(monitor.store.path, "SELECT service, status FROM service_probes")
        }
        assert statuses == {"ollama": "timeout", "stirling": "timeout"}


def test_collector_flush_delay_is_bounded_while_probes_time_out(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    """Flush and probes share the collector thread: the flush delay is bounded
    by the flush interval plus one Stirling and one Ollama deadline."""
    with (
        upstream("trickle") as url,
        monitored_client(monkeypatch, tmp_path, settings=_settings(tmp_path, url)) as client,
    ):
        monitor = client.monitor
        flushes = []
        original = monitor.store.write_batch

        def timed_write(*args, **kwargs):  # every collector flush ends in one write_batch
            flushes.append(time.monotonic())
            return original(*args, **kwargs)

        monkeypatch.setattr(monitor.store, "write_batch", timed_write)
        # the scheduler reads its due times every 0.5 s - make the next probes due now
        deadline = time.monotonic() + 5
        while not getattr(monitor, "_due", None) and time.monotonic() < deadline:
            time.sleep(0.05)
        for _ in range(3):
            monitor._due["probe"] = time.monotonic()
            client.get("/api/v1/health", headers=auth_headers(VALID_KEY))
            time.sleep(1.6)
        assert len(flushes) >= 3
        gaps = [b - a for a, b in zip(flushes, flushes[1:])]
        assert max(gaps) < 1 + 0.5 + 2 * TIMEOUT + SLACK  # interval + loop tick + two deadlines
        probes = db_rows(monitor.store.path, "SELECT COUNT(*) FROM service_probes WHERE status = 'timeout'")[
            0
        ][0]
        assert probes >= 2
        assert monitor.recorder.totals["requests_dropped_buffer"] == 0
