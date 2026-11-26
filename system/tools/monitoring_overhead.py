"""Measure the per-request overhead of the monitoring foundation (Phase M2).

Usage (local development only - synthetic requests, fake upstreams, a
temporary directory; no network, no real documents):

    python -m system.tools.monitoring_overhead [--requests 2000] [--rounds 5]

Method:
1. Micro benchmark: Monitor.begin() + end() for a synthetic HTTP scope with
   two processing steps, timed with perf_counter over N iterations. This
   isolates the monitoring code path from HTTP/test-client noise.
2. End to end: N in-process requests (GET /api/v1/health and a faked
   POST /api/v1/documents/extract-text) through the full ASGI stack via
   Starlette's TestClient, once with monitoring disabled and once enabled,
   interleaved over several rounds; reported are the median per-request
   wall times of each configuration and their difference.
3. Collector cost: time of one flush_now() persisting N buffered requests.

The numbers depend on the machine; they document an order of magnitude,
not a guarantee.
"""

from __future__ import annotations

import argparse
import dataclasses
import statistics
import tempfile
import time
from pathlib import Path


def _settings(tmp: Path, enabled: bool):
    from system.config import (
        ClientConfig,
        ClientServices,
        OllamaSettings,
        PromptSettings,
        ServerSettings,
        Settings,
        StirlingSettings,
    )
    from system.monitoring.settings import MonitoringSettings, ProbeSettings, StorageSettings

    return Settings(
        server=ServerSettings(),
        stirling=StirlingSettings(base_url="http://stirling.invalid", api_key="x", timeout_seconds=5),
        ollama=OllamaSettings(base_url=""),
        prompts=PromptSettings(runtime_dir=tmp / "prompts"),
        clients={
            "bench": ClientConfig(
                client_id="bench", enabled=True, api_key="bench-key", services=ClientServices(documents=True)
            )
        },
        monitoring=MonitoringSettings(
            enabled=enabled,
            data_dir=tmp / "mon",
            flush_interval_seconds=5,
            system_sample_interval_seconds=3600,
            probe_interval_seconds=3600,
            maintenance_interval_seconds=3600,
            probes=ProbeSettings(ollama=False, stirling=False),
            storage=StorageSettings(min_free_disk_bytes=0),
        ),
    )


class _FakeStirling:
    def __init__(self, settings) -> None:
        pass

    def close(self) -> None:
        pass

    def extract_text(self, file_bytes, filename):
        return "synthetic text " * 20


def micro(n: int, tmp: Path) -> float:
    from system.main import app
    from system.monitoring.context import StepRecord
    from system.monitoring.monitor import Monitor

    monitor = Monitor(_settings(tmp, True), env={})
    # A real route path with the real app: includes the route-template lookup.
    scope = {
        "type": "http",
        "path": "/api/v1/documents/extract-text",
        "method": "POST",
        "state": {},
        "app": app,
    }
    started = time.perf_counter()
    for _ in range(n):
        scope["state"] = {"client_id": "bench"}
        metrics = monitor.begin(scope)
        metrics.status_code = 200
        metrics.add_step(StepRecord(step="stirling.extract_text", outcome="ok", duration_ms=1.0))
        metrics.add_step(StepRecord(step="stirling.ocr", outcome="ok", duration_ms=2.0))
        monitor.end(metrics, scope)
        if len(monitor.recorder._requests) > 5000:
            monitor.recorder.take()
    return (time.perf_counter() - started) / n * 1e6


def end_to_end(n: int, rounds: int, tmp: Path) -> dict:
    from fastapi.testclient import TestClient

    from system import main

    results: dict[str, list[float]] = {"disabled": [], "enabled": []}
    original_settings, original_stirling = main.load_settings, main.StirlingClient
    main.StirlingClient = _FakeStirling
    pdf = b"%PDF-1.4\n" + b"0" * 2048
    try:
        for round_ in range(rounds):
            for label in ("disabled", "enabled"):
                settings = _settings(tmp / f"{label}-{round_}", label == "enabled")
                main.load_settings = lambda s=settings: s
                with TestClient(main.app) as client:
                    for i in range(n):
                        started = time.perf_counter()
                        if i % 2:
                            client.post(
                                "/api/v1/documents/extract-text",
                                headers={"Authorization": "Bearer bench-key"},
                                files={"file": ("a.pdf", pdf, "application/pdf")},
                            )
                        else:
                            client.get("/api/v1/health")
                        results[label].append((time.perf_counter() - started) * 1e6)
    finally:
        main.load_settings, main.StirlingClient = original_settings, original_stirling
    return {label: statistics.median(values) for label, values in results.items()}


def flush_cost(n: int, tmp: Path) -> float:
    from system.monitoring.monitor import Monitor
    from system.monitoring.recorder import FinishedRequest

    monitor = Monitor(_settings(tmp, True), env={})
    monitor.store.open()
    now = monitor._clock()
    record = FinishedRequest(
        request_id="",
        ts_ms=now,
        duration_ms=12.5,
        client_id="bench",
        route="/api/v1/health",
        method="GET",
        service="system",
        status_code=200,
        error_code=None,
        bytes_in=0,
        bytes_out=50,
        documents=0,
        pages=None,
        ai_provider=None,
        ai_model=None,
        ai_calls=0,
        tokens_in=None,
        tokens_out=None,
        steps_truncated=0,
    )
    for i in range(n):
        monitor.recorder.finish(dataclasses.replace(record, request_id=f"r{i}", ts_ms=now + i))
    started = time.perf_counter()
    monitor.flush_now()
    elapsed = (time.perf_counter() - started) * 1000
    monitor.store.close()
    return elapsed


def main_cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requests", type=int, default=2000)
    parser.add_argument("--rounds", type=int, default=5)
    args = parser.parse_args()
    import logging

    logging.disable(logging.INFO)
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        print(f"micro: begin()+end() with 2 steps: {micro(50_000, tmp / 'micro'):.1f} us per request")
        medians = end_to_end(args.requests, args.rounds, tmp / "e2e")
        print(
            f"end-to-end median, disabled: {medians['disabled']:.0f} us, "
            f"enabled: {medians['enabled']:.0f} us, "
            f"difference: {medians['enabled'] - medians['disabled']:.0f} us"
        )
        print(f"collector: flush of 5000 buffered requests: {flush_cost(5000, tmp / 'flush'):.0f} ms")


if __name__ == "__main__":
    main_cli()
