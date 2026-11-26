"""Thread-safe, bounded in-memory buffers between requests and the collector.

Requests never wait for the database: `finish()` appends a finished record
under a short lock and returns. The collector thread periodically swaps the
buffers out (`take()`) and persists them. Both buffers have a hard size
limit; on overflow the new item is dropped and counted per UTC minute, so
data completeness can be reported for every minute instead of silently
presenting partial statistics as complete.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field

from system.monitoring import registry

# Bound for the per-minute loss counters kept while the store is unreachable
# (one week of minutes); older minutes are merged into the oldest key kept.
MAX_LOSS_MINUTES = 7 * 24 * 60
EVENT_DEDUPE_WINDOW_MS = 60_000


def minute_of(ts_ms: int) -> int:
    return ts_ms - ts_ms % 60_000


@dataclass(slots=True)
class FinishedRequest:
    request_id: str
    ts_ms: int
    duration_ms: float
    client_id: str | None
    route: str
    method: str
    service: str
    status_code: int
    error_code: str | None
    bytes_in: int
    bytes_out: int
    documents: int
    pages: int | None
    ai_provider: str | None
    ai_model: str | None
    ai_calls: int
    tokens_in: int | None
    tokens_out: int | None
    steps_truncated: int
    steps: list = field(default_factory=list)


@dataclass(slots=True)
class EventRecord:
    first_ts_ms: int
    last_ts_ms: int
    severity: str
    code: str
    service: str
    client_id: str | None
    route: str | None
    error_code: str | None
    request_id: str | None
    detail: str | None
    repeat_count: int = 1


@dataclass(slots=True)
class QualityCounters:
    """Per-minute data-quality counters. `heartbeats` = collector cycles
    that ran in that minute: a minute without any marks a collector gap."""

    heartbeats: int = 0
    requests_dropped_buffer: int = 0
    requests_dropped_store: int = 0
    events_dropped: int = 0
    steps_truncated: int = 0
    flush_failures: int = 0
    task_failures: int = 0
    hook_failures: int = 0

    def merge(self, other: QualityCounters) -> None:
        for name in self.__slots__:
            setattr(self, name, getattr(self, name) + getattr(other, name))

    def any(self) -> bool:
        return any(getattr(self, name) for name in self.__slots__)

    def dropped_requests(self) -> int:
        return self.requests_dropped_buffer + self.requests_dropped_store


@dataclass
class Batch:
    requests: list[FinishedRequest]
    events: list[EventRecord]
    losses: dict[int, QualityCounters]


class Recorder:
    def __init__(self, *, max_requests: int, max_events: int) -> None:
        self._lock = threading.Lock()
        self._max_requests = max_requests
        self._max_events = max_events
        self._requests: list[FinishedRequest] = []
        self._events: OrderedDict[tuple, EventRecord] = OrderedDict()
        self._losses: dict[int, QualityCounters] = {}
        self._active: dict[str, tuple[str, str, str, int]] = {}
        # Lifetime totals of this process (exposed through health()).
        self.totals = {
            "requests_recorded": 0,
            "requests_dropped_buffer": 0,
            "requests_dropped_store": 0,
            "events_dropped": 0,
            "hook_failures": 0,
        }
        self._overflow_reported_minute = -1

    # --- request lifecycle -------------------------------------------------

    def begin(self, request_id: str, route: str, method: str, service: str, ts_ms: int) -> None:
        with self._lock:
            # Active-request tracking is bounded too; beyond the limit a
            # request is still measured, just not listed as "active".
            if len(self._active) < self._max_requests:
                self._active[request_id] = (route, method, service, ts_ms)

    def finish(self, record: FinishedRequest) -> None:
        overflow = False
        with self._lock:
            self._active.pop(record.request_id, None)
            if len(self._requests) >= self._max_requests:
                self._loss(minute_of(record.ts_ms)).requests_dropped_buffer += 1
                self.totals["requests_dropped_buffer"] += 1
                minute = minute_of(record.ts_ms)
                if self._overflow_reported_minute != minute:
                    self._overflow_reported_minute = minute
                    overflow = True
            else:
                self._requests.append(record)
                self.totals["requests_recorded"] += 1
                if record.steps_truncated:
                    self._loss(minute_of(record.ts_ms)).steps_truncated += record.steps_truncated
        if overflow:
            self.event("buffer_overflow", service="monitoring", ts_ms=record.ts_ms, detail="requests")

    def abandon(self, request_id: str) -> None:
        with self._lock:
            self._active.pop(request_id, None)

    def active_snapshot(self, limit: int = 500) -> list[tuple[str, str, str, str, int]]:
        with self._lock:
            items = list(self._active.items())[:limit]
        return [(rid, route, method, service, ts) for rid, (route, method, service, ts) in items]

    # --- events --------------------------------------------------------------

    def event(
        self,
        code: str,
        *,
        service: str,
        ts_ms: int,
        client_id: str | None = None,
        route: str | None = None,
        error_code: str | None = None,
        request_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        """Structured technical event. All values are registry members or
        config ids; repeats within a minute are counted, not stored again."""
        severity = registry.EVENT_CODES.get(code)
        if severity is None:
            code, severity = "hook_failed", registry.EVENT_CODES["hook_failed"]
        service = registry.member(service, registry.EVENT_SERVICES, "docpipe")
        if detail is not None:
            allowed = registry.COLLECTOR_TASKS | {"requests", "events"} | registry.PROBE_STATUSES
            detail = registry.member(detail, frozenset(allowed), registry.INVALID)
        key = (code, service, client_id, route, error_code, detail, ts_ms // EVENT_DEDUPE_WINDOW_MS)
        with self._lock:
            existing = self._events.get(key)
            if existing is not None:
                existing.repeat_count += 1
                existing.last_ts_ms = ts_ms
                existing.request_id = request_id or existing.request_id
                return
            if len(self._events) >= self._max_events:
                self._loss(minute_of(ts_ms)).events_dropped += 1
                self.totals["events_dropped"] += 1
                return
            self._events[key] = EventRecord(
                first_ts_ms=ts_ms,
                last_ts_ms=ts_ms,
                severity=severity,
                code=code,
                service=service,
                client_id=client_id,
                route=route,
                error_code=error_code,
                request_id=request_id,
                detail=detail,
            )

    # --- loss accounting -----------------------------------------------------

    def _loss(self, minute: int) -> QualityCounters:
        counters = self._losses.get(minute)
        if counters is not None:
            return counters
        if len(self._losses) >= MAX_LOSS_MINUTES:
            # Totals stay exact; only the time resolution of losses that
            # happen after a week without any successful write degrades.
            return self._losses[max(self._losses)]
        counters = self._losses[minute] = QualityCounters()
        return counters

    def count_loss(self, ts_ms: int, **increments: int) -> None:
        with self._lock:
            counters = self._loss(minute_of(ts_ms))
            for name, value in increments.items():
                setattr(counters, name, getattr(counters, name) + value)
                if name in self.totals:
                    self.totals[name] += value

    # --- collector side ------------------------------------------------------

    def take(self) -> Batch:
        with self._lock:
            batch = Batch(self._requests, list(self._events.values()), self._losses)
            self._requests, self._losses = [], {}
            self._events = OrderedDict()
        return batch

    def restore(self, batch: Batch, now_ms: int) -> None:
        """Put back a batch whose write failed. Requests beyond the buffer
        limit are dropped and counted as store losses (per their minute)."""
        with self._lock:
            room = max(0, self._max_requests - len(self._requests))
            kept, lost = batch.requests[:room], batch.requests[room:]
            self._requests = kept + self._requests
            for record in lost:
                self._loss(minute_of(record.ts_ms)).requests_dropped_store += 1
                self.totals["requests_dropped_store"] += 1
            for event in batch.events:
                if len(self._events) < self._max_events:
                    self._events[("restored", id(event))] = event
                else:
                    self._loss(minute_of(event.first_ts_ms)).events_dropped += 1
                    self.totals["events_dropped"] += 1
            for minute, counters in batch.losses.items():
                self._loss(minute).merge(counters)

    def pending(self) -> dict[str, int]:
        with self._lock:
            unsaved = QualityCounters()
            for counters in self._losses.values():
                unsaved.merge(counters)
            return {
                "requests_buffered": len(self._requests),
                "events_buffered": len(self._events),
                "active_requests": len(self._active),
                "unsaved_requests_dropped": unsaved.requests_dropped_buffer + unsaved.requests_dropped_store,
                "unsaved_events_dropped": unsaved.events_dropped,
            }
