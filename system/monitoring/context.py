"""Per-request metrics object and the hooks application code calls.

The ASGI middleware (system/monitoring/middleware.py) creates one
RequestMetrics per HTTP request and puts it into a contextvar. Starlette
runs the (sync) route functions through anyio's threadpool, which copies the
context - the worker thread therefore sees the same RequestMetrics object
and its additions are visible to the middleware when the response is done.

Every hook here is a no-op when no RequestMetrics is active (monitoring
disabled, or code running outside a request) and never raises: a broken
hook must not turn a successful document request into an error.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from system.monitoring import registry


@dataclass(slots=True)
class StepRecord:
    step: str
    outcome: str
    duration_ms: float
    # Ollama-reported values (ms / tokens) - None when not reported.
    ollama_total_ms: float | None = None
    ollama_load_ms: float | None = None
    ollama_prompt_eval_ms: float | None = None
    ollama_eval_ms: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    repair_used: bool = False
    ai_model: str | None = None


class RequestMetrics:
    __slots__ = (
        "request_id",
        "ts_start_ms",
        "mono_start",
        "route",
        "method",
        "service",
        "status_code",
        "error_code",
        "bytes_in",
        "bytes_out",
        "pages",
        "steps",
        "steps_truncated",
        "max_steps",
        "unhandled",
        "hook_failures",
    )

    def __init__(
        self,
        *,
        request_id: str,
        ts_start_ms: int,
        mono_start: float,
        route: str,
        method: str,
        service: str,
        max_steps: int,
    ) -> None:
        self.request_id = request_id
        self.ts_start_ms = ts_start_ms
        self.mono_start = mono_start
        self.route = route
        self.method = method
        self.service = service
        self.status_code: int | None = None
        self.error_code: str | None = None
        self.bytes_in = 0
        self.bytes_out = 0
        self.pages: int | None = None
        self.steps: list[StepRecord] = []
        self.steps_truncated = 0
        self.max_steps = max_steps
        self.unhandled = False
        self.hook_failures = 0

    def add_step(self, record: StepRecord) -> None:
        if len(self.steps) >= self.max_steps:
            self.steps_truncated += 1
            return
        self.steps.append(record)


_current: ContextVar[RequestMetrics | None] = ContextVar("docpipe_monitoring_request", default=None)


def current() -> RequestMetrics | None:
    return _current.get()


def activate(metrics: RequestMetrics):
    return _current.set(metrics)


def deactivate(token) -> None:
    _current.reset(token)


def note_error_code(code: str) -> None:
    """Called by the API error handlers with the normalized error code."""
    metrics = _current.get()
    if metrics is None:
        return
    try:
        metrics.error_code = registry.error_code(code)
    except Exception:
        metrics.hook_failures += 1


def note_pages(pages: object) -> None:
    """Page count a route already knows (forms) - never computed for monitoring."""
    metrics = _current.get()
    if metrics is None:
        return
    try:
        if isinstance(pages, int) and not isinstance(pages, bool) and 0 <= pages <= 1_000_000:
            metrics.pages = pages
    except Exception:
        metrics.hook_failures += 1


def add_step(metrics: RequestMetrics, record: StepRecord) -> None:
    try:
        metrics.add_step(record)
    except Exception:
        metrics.hook_failures += 1


@contextmanager
def step(name: str):
    """Time an in-process processing step of the current request."""
    metrics = _current.get()
    if metrics is None:
        yield
        return
    started = time.perf_counter()
    result = registry.OUTCOME_OK
    try:
        yield
    except Exception as exc:
        result = registry.outcome(getattr(exc, "code", None))
        raise
    finally:
        add_step(
            metrics,
            StepRecord(
                step=registry.member(name, registry.STEPS, registry.INVALID),
                outcome=result,
                duration_ms=round((time.perf_counter() - started) * 1000, 3),
            ),
        )
