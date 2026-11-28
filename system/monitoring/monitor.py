"""Monitoring runtime: request accounting, collector thread, lifecycle.

One Monitor per DocPipe process, created in system/main.py's lifespan only
when `monitoring.enabled` is true. It owns:

- the Recorder (bounded in-memory buffers, written to by requests),
- the MonitoringStore (SQLite, written only by the collector),
- the SystemSampler and ServiceProber,
- exactly one daemon collector thread, started in start() and joined in
  stop() - no other threads, no subprocesses.

Failure policy: the API never depends on monitoring. If the database cannot
be opened or written, data stays buffered (bounded) and is retried; what
does not fit is dropped and counted per minute, so incompleteness is
visible later instead of being presented as complete statistics. Each
collector task is isolated: one failing task neither stops the others nor
kills the thread.
"""

from __future__ import annotations

import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable

from system import VERSION
from system.monitoring import registry
from system.monitoring.buildinfo import read_build_info
from system.monitoring.context import RequestMetrics
from system.monitoring.probes import ServiceProber
from system.monitoring.recorder import FinishedRequest, Recorder
from system.monitoring.store import MonitoringStore
from system.monitoring.system_metrics import Sample, SystemSampler

THREAD_NAME = "docpipe-monitoring-collector"
_PARAM = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*(?::[a-z]+)?\}")
STORE_RETRY_SECONDS = 30.0
# Consecutive failed writes after which the connection is closed and reopened.
MAX_CONSECUTIVE_WRITE_FAILURES = 3
PRESSURE_RATIO = 0.9
LEASE_PROBES = "probes"
LEASE_SAMPLER = "system_sampler"


def _collect_route_patterns(app) -> list[tuple[re.Pattern, str]]:
    """(compiled path pattern, template) for every registered route, found by
    walking the router tree once. Templates are application code."""
    found: list[tuple[re.Pattern, str]] = []

    def walk(routes, depth: int) -> None:
        if depth > 5:
            return
        for route in routes:
            path = getattr(route, "path", None)
            if isinstance(path, str) and registry.safe_route(path):
                regex = (
                    "^" + _PARAM.sub("[^/]+", re.escape(path).replace(r"\{", "{").replace(r"\}", "}")) + "$"
                )
                found.append((re.compile(regex), path))
            inner = getattr(route, "original_router", None) or getattr(route, "routes", None)
            inner_routes = getattr(inner, "routes", inner)
            if isinstance(inner_routes, list) and inner_routes is not routes:
                walk(inner_routes, depth + 1)

    walk(list(getattr(getattr(app, "router", None), "routes", ())), 0)
    return found


def wall_ms() -> int:
    """UTC epoch milliseconds."""
    return time.time_ns() // 1_000_000


class Monitor:
    def __init__(
        self,
        settings,
        *,
        clock: Callable[[], int] = wall_ms,
        sampler: SystemSampler | None = None,
        prober: ServiceProber | None = None,
        env=None,
    ) -> None:
        self.settings = settings.monitoring
        self._clock = clock
        self.instance_id = uuid.uuid4().hex
        self.started_ms = clock()  # process start of monitoring (uptime)
        self.build = read_build_info(env)
        self.known_clients = frozenset(settings.clients)
        self.known_models = frozenset(p.model for p in settings.models.values())
        self.recorder = Recorder(
            max_requests=self.settings.buffer_max_requests, max_events=self.settings.buffer_max_events
        )
        self.store = MonitoringStore(self.settings)
        self.sampler = sampler or SystemSampler(data_dir=self.settings.data_dir)
        probes = self.settings.probes
        self.prober = prober or ServiceProber(
            ollama_url=settings.ollama.base_url,
            stirling_url=settings.stirling.base_url,
            probe_ollama=probes.ollama,
            probe_stirling=probes.stirling,
            timeout_seconds=probes.timeout_seconds,
        )
        self._store_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._writes_blocked: str | None = None
        self._store_failed_since: float | None = None
        self._last_store_attempt = 0.0
        self._write_failures = 0
        self._last_probe_status: dict[str, str] = {}
        self._due: dict[str, float] = {}
        self._route_patterns: list | None = None
        self.status = {
            "store": "closed",
            "collector": "stopped",
            "last_flush_ms": None,
            "last_flush_error": None,
            "last_sample_ms": None,
            "last_probe_ms": None,
            "last_maintenance_ms": None,
        }

    # --- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        now = self._clock()
        self._open_store()
        self.recorder.event("monitoring_started", service="monitoring", ts_ms=now)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()
        self.status["collector"] = "running"

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self.status["collector"] = "stopped" if not (self._thread and self._thread.is_alive()) else "stuck"
        self.recorder.event("monitoring_stopped", service="monitoring", ts_ms=self._clock())
        try:
            self.flush_now()
            if self.store.is_open:
                self.store.mark_stopped(self.instance_id, self._clock())
        except Exception:
            pass
        finally:
            self.store.close()
            self.prober.close()

    def _open_store(self) -> bool:
        self._last_store_attempt = time.monotonic()
        try:
            self.store.open()
            self.store.register_instance(
                instance_id=self.instance_id,
                pid=os.getpid(),
                now_ms=self._clock(),
                app_version=VERSION,
                build=self.build,
            )
        except Exception:
            self.store.close()
            if self.status["store"] != "unavailable":
                self.recorder.event("store_unavailable", service="monitoring", ts_ms=self._clock())
            self.status["store"] = "unavailable"
            return False
        if self.status["store"] == "unavailable":
            self.recorder.event("store_recovered", service="monitoring", ts_ms=self._clock())
        self.status["store"] = "open"
        return True

    # --- request side (called from the middleware, must be cheap) -------------

    def hook_failed(self) -> None:
        try:
            self.recorder.count_loss(self._clock(), hook_failures=1)
        except Exception:
            pass

    def _route_template(self, scope) -> str:
        """Template of the registered route a path belongs to (provisional:
        end() replaces it with the route the router actually dispatched to)."""
        if self._route_patterns is None:
            self._route_patterns = _collect_route_patterns(scope.get("app"))
        path = scope.get("path")
        if isinstance(path, str):
            for pattern, template in self._route_patterns:
                if pattern.match(path):
                    return template
        return registry.UNMATCHED_ROUTE

    def begin(self, scope) -> RequestMetrics:
        route = self._route_template(scope)
        method = registry.member(scope.get("method"), registry.METHODS, registry.OTHER_METHOD)
        service = registry.SERVICE_BY_ROUTE.get(
            route, "none" if route == registry.UNMATCHED_ROUTE else "other"
        )
        request_id = str(uuid.uuid4())
        # The existing access-log middleware reuses this id, so X-Request-ID,
        # the log line and the monitoring record correlate.
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        now = self._clock()
        metrics = RequestMetrics(
            request_id=request_id,
            ts_start_ms=now,
            mono_start=time.perf_counter(),
            route=route,
            method=method,
            service=service,
            max_steps=self.settings.max_steps_per_request,
        )
        metrics.state = state if isinstance(state, dict) else None
        self.recorder.begin(request_id, route, method, service, now)
        return metrics

    def authenticated_client(self, metrics: RequestMetrics) -> str | None:
        """The client id system/auth.py stored for this request, if it is a
        configured client - never a value from the request body or headers."""
        state = metrics.state
        raw = state.get("client_id") if isinstance(state, dict) else None
        return raw if isinstance(raw, str) and raw in self.known_clients else None

    def end(self, metrics: RequestMetrics, scope) -> None:
        duration_ms = round((time.perf_counter() - metrics.mono_start) * 1000, 3)
        # The route the router dispatched to (FastAPI sets scope["route"]);
        # its path is a template from application code, never the raw URL.
        dispatched = getattr(scope.get("route"), "path", None)
        if isinstance(dispatched, str) and registry.safe_route(dispatched):
            metrics.route = dispatched
            metrics.service = registry.SERVICE_BY_ROUTE.get(dispatched, "other")
        state = scope.get("state") or {}
        # Set only by system/auth.py after a successful API-key lookup -
        # never taken from the request body or headers.
        raw_client = state.get("client_id") if isinstance(state, dict) else None
        client_id = raw_client if raw_client in self.known_clients else None
        status = metrics.status_code
        error_code = metrics.error_code
        if metrics.unhandled and status is None:
            status, error_code = 500, "internal_error"
        status = status if isinstance(status, int) and 100 <= status <= 599 else 0
        ai_steps = [s for s in metrics.steps if s.step == "ai.generate"]
        tokens_known = bool(ai_steps) and all(
            s.tokens_in is not None and s.tokens_out is not None for s in ai_steps
        )
        models = {s.ai_model for s in metrics.steps if s.ai_model}
        record = FinishedRequest(
            request_id=metrics.request_id,
            ts_ms=metrics.ts_start_ms,
            duration_ms=duration_ms,
            client_id=client_id,
            route=metrics.route,
            method=metrics.method,
            service=metrics.service,
            status_code=status,
            error_code=error_code,
            bytes_in=metrics.bytes_in,
            bytes_out=metrics.bytes_out,
            documents=1 if status == 200 and metrics.route in registry.DOCUMENT_ROUTES else 0,
            pages=metrics.pages,
            ai_provider="ollama" if models else None,
            ai_model=models.pop() if len(models) == 1 else (registry.INVALID if models else None),
            ai_calls=len(ai_steps),
            tokens_in=sum(s.tokens_in for s in ai_steps) if tokens_known else None,
            tokens_out=sum(s.tokens_out for s in ai_steps) if tokens_known else None,
            steps_truncated=metrics.steps_truncated,
            steps=metrics.steps,
        )
        self.recorder.finish(record)
        if metrics.hook_failures:
            self.recorder.count_loss(metrics.ts_start_ms, hook_failures=metrics.hook_failures)
        if metrics.unhandled:
            self.recorder.event(
                "unhandled_exception",
                service="docpipe",
                ts_ms=metrics.ts_start_ms,
                client_id=client_id,
                route=metrics.route,
                error_code="internal_error",
                request_id=metrics.request_id,
            )
        elif error_code in registry.UPSTREAM_BY_ERROR:
            self.recorder.event(
                "upstream_error",
                service=registry.UPSTREAM_BY_ERROR[error_code],
                ts_ms=metrics.ts_start_ms,
                client_id=client_id,
                route=metrics.route,
                error_code=error_code,
                request_id=metrics.request_id,
            )

    # --- collector -----------------------------------------------------------------

    def _run(self) -> None:
        s = self.settings
        tasks = (
            ("flush", s.flush_interval_seconds, self.flush_now),
            ("sample", s.system_sample_interval_seconds, self.sample_now),
            ("probe", s.probe_interval_seconds, self.probe_now),
            ("maintenance", s.maintenance_interval_seconds, self.maintenance_now),
        )
        start = time.monotonic()
        self._due = {name: start + (0 if name == "flush" else interval) for name, interval, _ in tasks}
        while not self._stop.wait(0.5):
            for name, interval, fn in tasks:
                now = time.monotonic()
                if now < self._due[name]:
                    continue
                self._due[name] = now + interval
                try:
                    fn()
                except Exception:
                    self.recorder.count_loss(self._clock(), task_failures=1)
                    self.recorder.event(
                        "collector_task_failed", service="monitoring", ts_ms=self._clock(), detail=name
                    )
                if self._stop.is_set():
                    break

    def _storage_ok(self) -> bool:
        """Free disk space and size limit (db + -wal + -shm) check before a write."""
        storage = self.settings.storage
        try:
            free = shutil.disk_usage(self.settings.data_dir).free
        except OSError:
            free = None
        if free is not None and free < storage.min_free_disk_bytes:
            if self._writes_blocked != "disk":
                self.recorder.event("disk_space_low", service="monitoring", ts_ms=self._clock())
            self._writes_blocked = "disk"
            return False
        if self.store.file_bytes() >= storage.max_bytes:
            if self._writes_blocked != "size":
                self.recorder.event("storage_limit_reached", service="monitoring", ts_ms=self._clock())
            self._writes_blocked = "size"
            return False
        self._writes_blocked = None
        return True

    def flush_now(self) -> int:
        with self._store_lock:
            now = self._clock()
            self.recorder.count_loss(now, heartbeats=1)
            if not self.store.is_open and (
                time.monotonic() - self._last_store_attempt >= STORE_RETRY_SECONDS or self._stop.is_set()
            ):
                self._open_store()
            batch = self.recorder.take()
            if not self.store.is_open or not self._storage_ok():
                self._fail_batch(batch, now, count_failure=self.store.is_open is False)
                return 0
            try:
                persisted = self.store.write_batch(
                    self.instance_id, batch, now, self.recorder.active_snapshot(), self.recorder.ai_snapshot()
                )
            except Exception as exc:
                self.status["last_flush_error"] = type(exc).__name__
                self._fail_batch(batch, now, count_failure=True)
                if self.status["store"] != "write_failing":
                    self.recorder.event("store_unavailable", service="monitoring", ts_ms=now)
                self.status["store"] = "write_failing"
                self._write_failures += 1
                if self._write_failures >= MAX_CONSECUTIVE_WRITE_FAILURES:
                    # Possibly a broken connection: reopen on the retry schedule.
                    self.store.close()
                    self._write_failures = 0
                    self._last_store_attempt = time.monotonic()
                return 0
            self._write_failures = 0
            if self.status["store"] in ("write_failing", "unavailable"):
                self.recorder.event("store_recovered", service="monitoring", ts_ms=now)
            self.status.update(store="open", last_flush_ms=now, last_flush_error=None)
            return persisted

    def _fail_batch(self, batch, now: int, *, count_failure: bool) -> None:
        self.recorder.restore(batch, now)
        if count_failure:
            self.recorder.count_loss(now, flush_failures=1)

    def _lease(self, name: str, interval: float) -> bool:
        if not self.store.is_open:
            return False
        return self.store.acquire_lease(name, self.instance_id, self._clock(), int(interval * 3000))

    def sample_now(self) -> None:
        interval = self.settings.system_sample_interval_seconds
        with self._store_lock:
            if not self._lease(LEASE_SAMPLER, interval) or not self._storage_ok():
                return
            now = self._clock()
            samples = self.sampler.sample(time.monotonic())
            samples.append(Sample("monitoring.db_bytes", "monitoring", float(self.store.file_bytes())))
            self.store.write_samples(self.instance_id, now, samples)
            self.status["last_sample_ms"] = now

    def probe_now(self) -> None:
        with self._store_lock:
            if not self._lease(LEASE_PROBES, self.settings.probe_interval_seconds):
                return
        # Network calls outside the lock: a slow upstream never delays a flush.
        now = self._clock()
        stirling = self.prober.probe_stirling()
        ollama, models = self.prober.probe_ollama()
        for result in (stirling, ollama):
            previous = self._last_probe_status.get(result.service)
            if result.status not in ("ok", "not_configured") and previous != result.status:
                self.recorder.event(
                    "service_unreachable", service=result.service, ts_ms=now, detail=result.status
                )
            elif result.status == "ok" and previous not in (None, "ok"):
                self.recorder.event("service_recovered", service=result.service, ts_ms=now)
            self._last_probe_status[result.service] = result.status
        with self._store_lock:
            if self.store.is_open and self._storage_ok():
                self.store.write_probes(now, [stirling, ollama], models)
                self.status["last_probe_ms"] = now

    def maintenance_now(self) -> None:
        with self._store_lock:
            if not self.store.is_open:
                return
            now = self._clock()
            self.store.apply_retention(now)
            self.store.checkpoint()
            if self.store.file_bytes() >= self.settings.storage.max_bytes * PRESSURE_RATIO:
                self.recorder.event("storage_pressure", service="monitoring", ts_ms=now)
                self.store.prune_oldest()
                self.store.checkpoint()
            self.status["last_maintenance_ms"] = now

    # --- introspection ---------------------------------------------------------

    def health(self) -> dict:
        thread_alive = bool(self._thread and self._thread.is_alive())
        return {
            **self.status,
            "collector": "running" if thread_alive else ("stopped" if self._stop.is_set() else "dead"),
            "writes_blocked": self._writes_blocked,
            "instance_id": self.instance_id,
            "totals": dict(self.recorder.totals),
            **self.recorder.pending(),
        }
