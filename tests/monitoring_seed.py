"""Synthetic monitoring data for dashboard tests and screenshots.

Test-only: never imported by production code. Everything is generated with a
fixed seed; client ids, routes and models are generic placeholders.
"""

from __future__ import annotations

import random
import uuid

from system.monitoring.buildinfo import read_build_info
from system.monitoring.context import StepRecord
from system.monitoring.probes import OllamaModel, ProbeResult
from system.monitoring.recorder import Batch, EventRecord, FinishedRequest, QualityCounters
from system.monitoring.system_metrics import Sample

MINUTE = 60_000

ROUTES = [
    ("/api/v1/documents/extract-text", "documents", ["stirling.extract_text"]),
    (
        "/api/v1/documents/extract-text",
        "documents",
        ["stirling.extract_text", "stirling.ocr", "stirling.extract_text"],
    ),
    ("/api/v1/documents/convert-to-pdf", "documents", ["stirling.convert"]),
    ("/api/v1/documents/analyze", "ai", ["ai.generate"]),
    ("/api/v1/documents/forms/inspect", "forms", ["forms.inspect", "stirling.ocr"]),
    ("/api/v1/documents/forms/fill", "forms", ["forms.fill"]),
    ("/api/v1/health", "system", []),
]
STEP_MS = {
    "stirling.extract_text": 180,
    "stirling.ocr": 2400,
    "stirling.convert": 900,
    "ai.generate": 6500,
    "forms.inspect": 3200,
    "forms.fill": 450,
}


def synthetic_request(
    rng: random.Random,
    ts: int,
    client: str | None,
    route_index: int | None = None,
    model: str = "synthetic-model",
) -> FinishedRequest:
    route, service, steps = ROUTES[route_index if route_index is not None else rng.randrange(len(ROUTES))]
    status = rng.choices([200, 200, 200, 200, 200, 400, 401, 502, 504], k=1)[0] if client else 401
    if not client:
        steps = []
    error = {400: "invalid_file", 401: "unauthorized", 502: "upstream_unavailable", 504: "timeout"}.get(
        status
    )
    records, total = [], 2.0
    for step in steps:
        dur = max(1.0, rng.lognormvariate(0, 0.45) * STEP_MS[step])
        rec = StepRecord(step=step, outcome="ok" if status < 500 else "timeout", duration_ms=round(dur, 1))
        if step == "ai.generate":
            load = rng.choice([0.0, 0.0, 0.0, 1800.0])
            rec = StepRecord(
                step=step,
                outcome=rec.outcome,
                duration_ms=round(dur + load, 1),
                ollama_total_ms=round(dur + load - 20, 1),
                ollama_load_ms=load,
                ollama_prompt_eval_ms=round(dur * 0.35, 1),
                ollama_eval_ms=round(dur * 0.6, 1),
                tokens_in=rng.randrange(800, 4000),
                tokens_out=rng.randrange(80, 400),
                ai_model=model,
            )
        records.append(rec)
        if step != "stirling.ocr" or "forms.inspect" not in steps:
            total += rec.duration_ms
    ai = [r for r in records if r.step == "ai.generate"]
    return FinishedRequest(
        request_id=str(uuid.UUID(int=rng.getrandbits(128))),
        ts_ms=ts,
        duration_ms=round(total, 1),
        client_id=client,
        route=route,
        method="POST" if service != "system" else "GET",
        service=service,
        status_code=status,
        error_code=error,
        bytes_in=rng.randrange(2_000, 3_000_000),
        bytes_out=rng.randrange(200, 90_000),
        documents=1 if status == 200 and service in ("documents", "forms") else 0,
        pages=rng.randrange(1, 12) if service == "forms" and status == 200 else None,
        ai_provider="ollama" if ai else None,
        ai_model=model if ai else None,
        ai_calls=len(ai),
        tokens_in=sum(r.tokens_in for r in ai) if ai else None,
        tokens_out=sum(r.tokens_out for r in ai) if ai else None,
        steps_truncated=0,
        steps=records,
    )


def seed(
    store,
    *,
    now_ms: int,
    minutes: int = 180,
    clients=("client-alpha", "client-beta"),
    per_minute: int = 6,
    gap: tuple[int, int] | None = None,
    dropped_minute: int | None = None,
    instance_id: str = "seed-instance",
    seed_value: int = 7,
) -> dict:
    """Write `minutes` of synthetic history ending at now_ms. `gap` = (from, to)
    minutes ago without collector heartbeats and requests; `dropped_minute` =
    minutes ago with 3 dropped requests. Returns what was written."""
    rng = random.Random(seed_value)
    start = now_ms - minutes * MINUTE
    start -= start % MINUTE
    store.register_instance(
        instance_id=instance_id,
        pid=1,
        now_ms=start - MINUTE,
        app_version="0.1.0",
        build=read_build_info(
            {
                "DOCPIPE_BUILD_COMMIT": "0123abc",
                "DOCPIPE_IMAGE_TAG": "synthetic-tag",
                "DOCPIPE_BUILD_LOCAL_PATCHES": "example-patch",
            }
        ),
    )
    written = []
    for m in range(minutes):
        minute_ts = start + m * MINUTE
        ago = (now_ms - minute_ts) // MINUTE
        if gap and gap[1] <= ago <= gap[0]:
            continue
        load = per_minute * (1.6 if 60 <= m % 180 <= 90 else 1.0)
        requests = []
        for _ in range(rng.randrange(int(load * 0.5), int(load * 1.5) + 1)):
            client = rng.choices(
                [*clients, None], weights=[6, 3, *([2] * (len(clients) - 2)), 1][: len(clients) + 1]
            )[0]
            requests.append(synthetic_request(rng, minute_ts + rng.randrange(0, MINUTE), client))
        losses = {minute_ts: QualityCounters(heartbeats=12)}
        if dropped_minute is not None and ago == dropped_minute:
            losses[minute_ts].requests_dropped_buffer = 3
        events = []
        if any(r.error_code == "upstream_unavailable" for r in requests):
            events.append(
                EventRecord(
                    minute_ts,
                    minute_ts,
                    "WARNING",
                    "upstream_error",
                    "stirling",
                    None,
                    "/api/v1/documents/extract-text",
                    "upstream_unavailable",
                    None,
                    None,
                )
            )
        store.write_batch(instance_id, Batch(requests, events, losses), minute_ts + MINUTE - 1, [])
        written += requests
        samples = [
            Sample(
                "host.cpu.percent",
                "host",
                round(20 + 25 * rng.random() + (20 if 60 <= m % 180 <= 90 else 0), 1),
            ),
            Sample("host.cpu.cores", "host", 8.0),
            Sample("host.load1", "host", round(1 + 3 * rng.random(), 2)),
            Sample("host.memory.total_bytes", "host", 16 * 2**30),
            Sample("host.memory.available_bytes", "host", (6 + rng.random()) * 2**30),
            Sample("host.memory.used_percent", "host", round(58 + 6 * rng.random(), 1)),
            Sample("container.memory.current_bytes", "container", (0.6 + 0.2 * rng.random()) * 2**30),
            Sample("container.memory.limit_bytes", "container", None, "not_applicable", "unlimited"),
            Sample("container.cpu.cores_used", "container", round(0.2 + rng.random(), 2)),
            Sample("process.rss_bytes", "process", (210 + 30 * rng.random()) * 2**20),
            Sample("process.cpu.percent", "process", round(5 + 20 * rng.random(), 1)),
            Sample("disk.data.total_bytes", "filesystem", 200 * 2**30),
            Sample("disk.data.free_bytes", "filesystem", (120 - m * 0.001) * 2**30),
            Sample("disk.root.free_bytes", "filesystem", 90 * 2**30),
            Sample("net.rx_bytes_per_s", "network_namespace", round(rng.random() * 400_000)),
            Sample("net.tx_bytes_per_s", "network_namespace", round(rng.random() * 150_000)),
            Sample("gpu.utilization_percent", "gpu", None, "unavailable", "not_configured"),
            Sample("gpu.memory_used_bytes", "gpu", None, "unavailable", "not_configured"),
        ]
        store.write_samples(instance_id, minute_ts + 30_000, samples)
        if m % 2 == 0:
            store.write_probes(
                minute_ts + 10_000,
                [
                    ProbeResult("stirling", "ok", 200, round(8 + 5 * rng.random(), 1), "3.2.2"),
                    ProbeResult("ollama", "ok", 200, round(3 + 2 * rng.random(), 1), "0.40.18"),
                ],
                [
                    OllamaModel(
                        "synthetic-model", True, True, 1_200_000_000, 0, "qwen2", "1.5B", "Q4_K_M", 8192, None
                    )
                ],
            )
    store._db().execute(
        "UPDATE instances SET last_heartbeat_ms = ? WHERE instance_id = ?", (now_ms, instance_id)
    )
    return {"requests": written, "start_ms": start}


def seed_audit(auth_store, *, now_ms: int) -> None:
    for i in range(12):
        ts = now_ms - (i * 47 + 5) * MINUTE
        auth_store.audit(ts, "login_success", username="alice", ip="10.8.0.7", session_ref=f"{i:012x}")
    for i in range(7):
        auth_store.audit(now_ms - (i * 91 + 3) * MINUTE, "login_failed", ip="10.8.0.9", reason="credentials")
    auth_store.audit(now_ms - 200 * MINUTE, "mfa_failed", username="alice", ip="10.8.0.7")
    auth_store.audit(now_ms - 30 * MINUTE, "access_denied", ip="198.51.100.4", reason="network_not_allowed")
    auth_store.audit(now_ms - 150 * MINUTE, "lockout", ip="10.8.0.9", reason="ip")
    auth_store.audit(now_ms - 400 * MINUTE, "session_expired", username="alice", reason="idle")
