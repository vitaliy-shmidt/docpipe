"""Read-only queries behind the monitoring dashboard (Phase M4).

Rules this module enforces:

- One data source per query: raw rows, minute rollups or hour rollups,
  chosen from the requested range and the retention. Totals, series and
  percentiles of one answer always come from the same source, so raw rows
  and rollups are never added up (no double counting). The answer states the
  source, the bucket size and the effective (bucket-aligned) start.
- Percentiles are exact only from raw rows; from rollups they are histogram
  estimates and are labelled `approximate`.
- A time bucket is only reported as 0 when the collector was alive in it
  (heartbeats in `data_quality`). Without any heartbeat it is a gap (null);
  partly covered buckets carry their coverage. Nothing is interpolated.
- Rates are null when the denominator is 0.
- Only validated filter values reach SQL, always as bound parameters.
- A read-only connection (`mode=ro`, `query_only`) is used; in WAL mode it
  never blocks the collector's writes. Every query has a time budget.
- Nothing here contacts Ollama or Stirling: service health is read from the
  probe results the collector already stored.
"""

from __future__ import annotations

import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from system import VERSION
from system.monitoring import registry
from system.monitoring.stats import BUCKET_COLUMNS, PERCENTILES, exact_percentile, histogram_percentile

MINUTE = 60_000
HOUR = 3_600_000
DAY = 86_400_000
RANGES = {"live": 15 * MINUTE, "1h": HOUR, "24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY}
MAX_POINTS = 120  # per series: readable charts, bounded payloads
NICE_BUCKETS = (
    10_000,
    30_000,
    MINUTE,
    5 * MINUTE,
    15 * MINUTE,
    30 * MINUTE,
    HOUR,
    2 * HOUR,
    3 * HOUR,
    6 * HOUR,
    12 * HOUR,
    DAY,
)
RAW_EXACT_LIMIT = 200_000
QUERY_BUDGET_SECONDS = 3.0
MAX_PAGE = 100
TOP_CLIENTS = 6
UNAUTHENTICATED = "unauthenticated"
STATUS_FILTERS = {
    "success": "status_code BETWEEN 100 AND 399",
    "failed": "(status_code >= 400 OR status_code < 100)",
    "client_error": "status_code BETWEEN 400 AND 499",
    "server_error": "status_code >= 500",
}
OVERVIEW_METRICS = (
    "host.cpu.percent",
    "host.memory.used_percent",
    "container.cpu.cores_used",
    "container.memory.current_bytes",
)
SERVER_METRICS = (
    "host.cpu.percent",
    "host.load1",
    "host.memory.used_percent",
    "host.memory.available_bytes",
    "container.cpu.cores_used",
    "container.memory.current_bytes",
    "process.rss_bytes",
    "process.cpu.percent",
    "disk.data.free_bytes",
    "disk.root.free_bytes",
    "net.rx_bytes_per_s",
    "net.tx_bytes_per_s",
    "gpu.utilization_percent",
    "gpu.memory_used_bytes",
)
# Step nesting: the parent's duration includes the child's (inclusive times).
STEP_PARENTS = {"stirling.ocr": "forms.inspect"}
AUDIT_CATEGORIES = {
    "login_success": "login",
    "login_failed": "failure",
    "mfa_failed": "failure",
    "totp_replay": "failure",
    "lockout": "lockout",
    "login_throttled": "lockout",
    "logout": "session",
    "session_expired": "session",
    "session_revoked": "session",
    "access_denied": "denied",
    "csrf_failed": "denied",
    "security_config_error": "config",
}


class BadRequest(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TelemetryUnavailable(Exception):
    pass


@dataclass(frozen=True)
class Range:
    key: str
    start: int
    end: int
    source: str  # raw | minute | hour
    bucket: int
    first_bucket: int

    @property
    def resolution(self) -> int:
        return MINUTE if self.source == "minute" else HOUR

    def describe(self) -> dict:
        return {
            "key": self.key,
            "start_ms": self.start,
            "end_ms": self.end,
            "source": self.source,
            "bucket_ms": self.bucket,
            "effective_start_ms": self.first_bucket,
            "approximate": self.source != "raw",
        }


def _int(value, name: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise BadRequest(f"invalid_{name}") from exc
    return number


def _floor(value: int, step: int) -> int:
    return value - value % step


def _rate(part: int | None, total: int | None) -> float | None:
    return part / total if total else None


def _pct(durations: list[float]) -> dict:
    return {name: exact_percentile(durations, p) for name, p in PERCENTILES.items()}


def _hist_pct(counts: list[int], lo, hi) -> dict:
    return {name: histogram_percentile(counts, p, lo, hi) for name, p in PERCENTILES.items()}


class Dashboard:
    def __init__(self, monitoring_settings, monitor, auth_store, known_clients, *, clock) -> None:
        self.settings = monitoring_settings
        self.monitor = monitor
        self.auth = auth_store
        self.known_clients = frozenset(known_clients)
        self._clock = clock
        self._path = Path(monitoring_settings.db_path).resolve()
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._deadline = 0.0

    # --- connection -------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            if not self._path.exists():
                raise TelemetryUnavailable()
            try:
                conn = sqlite3.connect(
                    f"file:{self._path.as_posix()}?mode=ro", uri=True, timeout=2.0, check_same_thread=False
                )
                conn.execute("PRAGMA query_only = 1")
                conn.set_progress_handler(self._check_budget, 20_000)
            except sqlite3.Error as exc:
                raise TelemetryUnavailable() from exc
            self._conn = conn
        return self._conn

    def _check_budget(self) -> int:
        return 1 if time.monotonic() > self._deadline else 0  # non-zero aborts the statement

    def query(self, fn):
        """Run `fn(conn)` with the lock and the time budget; telemetry
        problems become TelemetryUnavailable (fail-open for the API, a clear
        'not available' for the dashboard)."""
        with self._lock:
            try:
                conn = self._connect()
                self._deadline = time.monotonic() + QUERY_BUDGET_SECONDS
                return fn(conn)
            except sqlite3.Error as exc:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
                raise TelemetryUnavailable() from exc

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # --- parameters -------------------------------------------------------------------

    def parse_range(self, params: dict, now: int, *, default: str = "1h") -> Range:
        key = params.get("range", default)
        retention = self.settings.retention
        oldest = now - retention.hour_rollup_days * DAY
        if key == "custom":
            start, end = _int(params.get("from"), "from"), _int(params.get("to"), "to")
            if end > now + MINUTE:
                end = now
            if start < oldest or end - start < MINUTE or start >= end:
                raise BadRequest("invalid_range")
        elif key in RANGES:
            start, end = now - RANGES[key], now
        else:
            raise BadRequest("invalid_range")
        span = end - start
        if span <= HOUR + MINUTE and start >= now - retention.raw_days * DAY:
            source, base = "raw", 10_000
        elif span <= DAY + MINUTE and start >= now - retention.minute_rollup_days * DAY:
            source, base = "minute", MINUTE
        else:
            source, base = "hour", HOUR
        bucket = next(
            (b for b in NICE_BUCKETS if b >= base and b % base == 0 and span / b <= MAX_POINTS),
            NICE_BUCKETS[-1],
        )
        first = _floor(start, base) if source != "raw" else start
        return Range(key, start, end, source, bucket, _floor(first, bucket))

    def _filters(self, params: dict, *, raw: bool, allow: tuple[str, ...]) -> tuple[str, list]:
        sql, args = "", []
        unknown = set(params) - {"range", "from", "to", "cursor", "limit", "category", *allow}
        if unknown:
            raise BadRequest("unknown_parameter")
        client = params.get("client")
        if client:
            if client == UNAUTHENTICATED:
                sql += " AND client_id IS NULL" if raw else " AND client_id = ''"
            elif client in self.known_clients:
                sql += " AND client_id = ?"
                args.append(client)
            else:
                raise BadRequest("invalid_client")
        service = params.get("service")
        if service:
            if service not in registry.SERVICES:
                raise BadRequest("invalid_service")
            sql += " AND service = ?"
            args.append(service)
        route = params.get("route")
        if route:
            if route not in registry.SERVICE_BY_ROUTE and route != registry.UNMATCHED_ROUTE:
                raise BadRequest("invalid_route")
            sql += " AND route = ?"
            args.append(route)
        status = params.get("status")
        if status:
            if status not in STATUS_FILTERS:
                raise BadRequest("invalid_status")
            sql += " AND " + STATUS_FILTERS[status]
        return sql, args

    # --- shared building blocks -----------------------------------------------------------

    def _coverage(self, conn, rng: Range, now: int) -> dict[int, float | None]:
        """Bucket index -> share (0..1) of the bucket's minutes in which the
        collector demonstrably ran (heartbeats in data_quality). None = unknown:
        outside the data-quality retention, or minutes not written yet (after
        the latest heartbeat). Works for buckets shorter and longer than a minute."""
        quality_from = now - self.settings.retention.data_quality_days * DAY
        lo_all = _floor(max(rng.first_bucket, rng.start), MINUTE)
        covered = {
            m
            for (m,) in conn.execute(
                "SELECT DISTINCT bucket_ms FROM data_quality WHERE heartbeats > 0 AND bucket_ms >= ? AND "
                "bucket_ms < ?",
                (lo_all, rng.end),
            )
        }
        latest = conn.execute("SELECT MAX(bucket_ms) FROM data_quality WHERE heartbeats > 0").fetchone()[0]
        written_until = (latest + MINUTE) if latest is not None else None
        result: dict[int, float | None] = {}
        for index in range(self._bucket_count(rng)):
            b_start = rng.first_bucket + index * rng.bucket
            lo, hi = max(b_start, rng.start), min(b_start + rng.bucket, rng.end, now)
            if hi <= lo or b_start < quality_from:
                result[index] = None
                continue
            minutes = range(_floor(lo, MINUTE), hi, MINUTE)
            known = [m for m in minutes if written_until is None or m < written_until]
            if not known:
                result[index] = None  # not written yet (or no data at all): unknown, not a gap
                continue
            result[index] = sum(1 for m in known if m in covered) / len(known)
        return result

    def _bucket_count(self, rng: Range) -> int:
        return max(1, math.ceil((rng.end - rng.first_bucket) / rng.bucket))

    def completeness(self, conn, start: int, end: int, now: int) -> dict:
        first = _floor(start, MINUTE)
        sums = conn.execute(
            "SELECT SUM(requests_persisted), SUM(requests_dropped_buffer), SUM(requests_dropped_store), "
            "SUM(events_dropped), SUM(steps_truncated), SUM(flush_failures), SUM(task_failures), "
            "SUM(hook_failures) FROM data_quality WHERE bucket_ms >= ? AND bucket_ms < ?",
            (first, end),
        ).fetchone()
        persisted, d_buffer, d_store, events_dropped, truncated, flush_f, task_f, hook_f = (
            v or 0 for v in sums
        )
        gaps = 0
        for instance_id, started, stopped in conn.execute(
            "SELECT instance_id, started_ms, stopped_ms FROM instances WHERE started_ms < ? "
            "AND COALESCE(stopped_ms, ?) >= ?",
            (end, now, start),
        ):
            lo = max(first, _floor(started, MINUTE) + MINUTE)
            hi = min(_floor(min(end, now), MINUTE), _floor(stopped or now, MINUTE))
            if hi <= lo:
                continue
            covered = conn.execute(
                "SELECT COUNT(*) FROM data_quality WHERE instance_id = ? AND bucket_ms >= ? "
                "AND bucket_ms < ? AND heartbeats > 0",
                (instance_id, lo, hi),
            ).fetchone()[0]
            gaps += (hi - lo) // MINUTE - covered
        oldest_heartbeat = conn.execute(
            "SELECT MIN(bucket_ms) FROM data_quality WHERE heartbeats > 0"
        ).fetchone()[0]
        unsaved = 0
        try:
            health = self.monitor.health() if self.monitor is not None else {}
            unsaved = int(health.get("unsaved_requests_dropped") or 0) if end >= now - MINUTE else 0
        except Exception:
            pass
        dropped = d_buffer + d_store + unsaved
        before_data = oldest_heartbeat is None or start < oldest_heartbeat - MINUTE
        return {
            "complete": dropped == 0 and gaps == 0 and not before_data,
            "requests_persisted": persisted,
            "requests_dropped": dropped,
            "requests_dropped_buffer": d_buffer,
            "requests_dropped_store": d_store,
            "requests_dropped_unsaved": unsaved,
            "events_dropped": events_dropped,
            "steps_truncated": truncated,
            "collector_gap_minutes": gaps,
            "flush_failures": flush_f,
            "task_failures": task_f,
            "hook_failures": hook_f,
            "range_before_first_data": before_data,
            "first_data_ms": oldest_heartbeat,
            "recorded_ratio": _rate(persisted, persisted + dropped),
        }

    def _request_buckets(self, conn, rng: Range, where: str, args: list, *, group_client: bool = False):
        """Aggregates per bucket (and optionally per client) from ONE source."""
        ok = "CASE WHEN status_code BETWEEN 100 AND 399 THEN {n} ELSE 0 END"
        fail = "CASE WHEN status_code >= 400 OR status_code < 100 THEN {n} ELSE 0 END"
        srv = "CASE WHEN status_code >= 500 THEN {n} ELSE 0 END"
        client_col = ", COALESCE(NULLIF(client_id, ''), NULL)" if group_client else ""
        if rng.source == "raw":
            sql = (
                f"SELECT (ts_ms - ?) / ? AS b{client_col}, COUNT(*), SUM({ok.format(n=1)}), "
                f"SUM({fail.format(n=1)}), SUM({srv.format(n=1)}), SUM(duration_ms), MIN(duration_ms), "
                "MAX(duration_ms), SUM(bytes_in), SUM(bytes_out), SUM(documents), SUM(COALESCE(pages, 0)), "
                "SUM(pages IS NOT NULL), SUM(ai_calls), SUM(CASE WHEN tokens_in IS NOT NULL AND tokens_out "
                "IS NOT NULL "
                "THEN tokens_in ELSE 0 END), SUM(CASE WHEN tokens_in IS NOT NULL AND tokens_out IS NOT NULL "
                "THEN tokens_out ELSE 0 END), SUM(tokens_in IS NOT NULL AND tokens_out IS NOT NULL AND "
                "ai_calls > 0) "
                f"FROM request_events WHERE ts_ms >= ? AND ts_ms < ?{where} GROUP BY "
                f"b{', 2' if group_client else ''}"
            )
            params = [rng.first_bucket, rng.bucket, rng.start, rng.end, *args]
        else:
            hist = ", ".join(f"SUM({c})" for c in BUCKET_COLUMNS)
            sql = (
                f"SELECT (bucket_ms - ?) / ? AS b{client_col}, SUM(count), SUM({ok.format(n='count')}), "
                f"SUM({fail.format(n='count')}), SUM({srv.format(n='count')}), SUM(dur_sum), MIN(dur_min), "
                "MAX(dur_max), SUM(bytes_in), SUM(bytes_out), SUM(documents), SUM(pages), SUM(pages_known), "
                f"SUM(ai_calls), SUM(tokens_in), SUM(tokens_out), SUM(tokens_known), {hist} "
                "FROM rollup_requests WHERE resolution_ms = ? AND bucket_ms >= ? AND bucket_ms < ?"
                f"{where} GROUP BY b{', 2' if group_client else ''}"
            )
            params = [rng.first_bucket, rng.bucket, rng.resolution, rng.first_bucket, rng.end, *args]
        return conn.execute(sql, params).fetchall()

    def _summarize(self, rows: list[tuple], offset: int) -> dict:
        """Totals over buckets; `offset` = columns before the numeric fields."""
        total = [0] * 16
        hist = [0] * len(BUCKET_COLUMNS)
        lo = hi = None
        for row in rows:
            values = row[offset:]
            for i in (0, 1, 2, 3, 4, 7, 8, 9, 10, 11, 12, 13, 14, 15):
                total[i] += values[i] or 0
            if values[5] is not None:
                lo = values[5] if lo is None else min(lo, values[5])
            if values[6] is not None:
                hi = values[6] if hi is None else max(hi, values[6])
            if len(values) > 16:
                for i, c in enumerate(values[16:]):
                    hist[i] += c or 0
        count, ok, failed, server_errors = total[0], total[1], total[2], total[3]
        return {
            "count": count,
            "success": ok,
            "failed": failed,
            "server_errors": server_errors,
            "success_rate": _rate(ok, count),
            "failure_rate": _rate(failed, count),
            "avg_ms": total[4] / count if count else None,
            "min_ms": lo,
            "max_ms": hi,
            "bytes_in": total[7],
            "bytes_out": total[8],
            "documents": total[9],
            "pages": total[10],
            "pages_known": total[11],
            "ai_calls": total[12],
            "tokens_in": total[13],
            "tokens_out": total[14],
            "tokens_known_requests": total[15],
            "_hist": hist,
        }

    def _percentiles(self, conn, rng: Range, where: str, args: list, summary: dict) -> dict:
        if rng.source == "raw":
            durations = [
                d
                for (d,) in conn.execute(
                    f"SELECT duration_ms FROM request_events WHERE ts_ms >= ? AND ts_ms < ?{where} ORDER BY "
                    "duration_ms",
                    (rng.start, rng.end, *args),
                )
            ]
            return {**_pct(durations), "method": "exact"}
        return {**_hist_pct(summary["_hist"], summary["min_ms"], summary["max_ms"]), "method": "histogram"}

    def _series(self, rows: list[tuple], rng: Range, coverage: dict, per_bucket_pct: dict) -> list[dict]:
        by_bucket = {row[0]: row for row in rows}
        points = []
        for index in range(self._bucket_count(rng)):
            cov = coverage.get(index)
            row = by_bucket.get(index)
            t = rng.first_bucket + index * rng.bucket
            if row is None:
                # Zero only where the collector demonstrably ran; otherwise a gap.
                value = 0 if cov else None
                points.append(
                    {
                        "t": t,
                        "count": value,
                        "success": value,
                        "failed": value,
                        "avg_ms": None,
                        "p95_ms": None,
                        "coverage": cov,
                    }
                )
                continue
            count = row[1] or 0
            points.append(
                {
                    "t": t,
                    "count": count,
                    "success": row[2] or 0,
                    "failed": row[3] or 0,
                    "avg_ms": (row[5] or 0) / count if count else None,
                    "p95_ms": per_bucket_pct.get(index),
                    "coverage": cov,
                }
            )
        return points

    def _bucket_p95(self, conn, rng: Range, rows, where, args) -> dict[int, float | None]:
        if rng.source != "raw":
            return {row[0]: histogram_percentile(list(row[17:]), 0.95, row[6], row[7]) for row in rows}
        result: dict[int, list] = {}
        for b, d in conn.execute(
            f"SELECT (ts_ms - ?) / ?, duration_ms FROM request_events WHERE ts_ms >= ? "
            f"AND ts_ms < ?{where} ORDER BY 1, 2",
            (rng.first_bucket, rng.bucket, rng.start, rng.end, *args),
        ):
            result.setdefault(b, []).append(d)
        return {b: exact_percentile(v, 0.95) for b, v in result.items()}

    def _raw_too_large(self, conn, rng: Range, where: str, args: list) -> bool:
        if rng.source != "raw":
            return False
        count = conn.execute(
            f"SELECT COUNT(*) FROM request_events WHERE ts_ms >= ? AND ts_ms < ?{where}",
            (rng.start, rng.end, *args),
        ).fetchone()[0]
        return count > RAW_EXACT_LIMIT

    def _effective_range(self, conn, rng: Range, where, args) -> Range:
        if self._raw_too_large(conn, rng, where, args):
            return Range(
                rng.key,
                rng.start,
                rng.end,
                "minute",
                max(rng.bucket, MINUTE),
                _floor(_floor(rng.start, MINUTE), max(rng.bucket, MINUTE)),
            )
        return rng

    # --- service health (stored data only) ------------------------------------------------

    def services(self, conn, now: int) -> list[dict]:
        probe_interval = self.settings.probe_interval_seconds * 1000
        stale_after = 3 * probe_interval + 30_000
        out = [
            {
                "service": "docpipe",
                "state": "online",
                "source": "docpipe",
                "version": VERSION,
                "last_check_ms": now,
                "last_ok_ms": now,
            }
        ]
        health = {}
        try:
            health = self.monitor.health() if self.monitor is not None else {}
        except Exception:
            pass
        flush_ms = health.get("last_flush_ms")
        flush_stale = (
            flush_ms is None or now - flush_ms > 3 * self.settings.flush_interval_seconds * 1000 + 5000
        )
        collector = health.get("collector")
        state = (
            "offline" if collector in ("dead", "stopped", None) else ("stale" if flush_stale else "online")
        )
        if state == "online" and (health.get("store") != "open" or health.get("writes_blocked")):
            state = "degraded"
        out.append(
            {
                "service": "collector",
                "state": state,
                "source": "docpipe",
                "detail": collector,
                "store": health.get("store"),
                "last_check_ms": now,
                "last_ok_ms": flush_ms,
            }
        )
        for service, enabled in (
            ("ollama", self.settings.probes.ollama),
            ("stirling", self.settings.probes.stirling),
        ):
            last = conn.execute(
                "SELECT ts_ms, status, latency_ms, version FROM service_probes WHERE service = ? "
                "ORDER BY ts_ms DESC LIMIT 1",
                (service,),
            ).fetchone()
            last_ok = conn.execute(
                "SELECT MAX(ts_ms) FROM service_probes WHERE service = ? AND status = 'ok'", (service,)
            ).fetchone()[0]
            errors = conn.execute(
                "SELECT COALESCE(SUM(repeat_count), 0) FROM events WHERE service = ? "
                "AND code = 'upstream_error' AND last_ts_ms >= ?",
                (service, now - 15 * MINUTE),
            ).fetchone()[0]
            if not enabled or (last is not None and last[1] == "not_configured"):
                state = "not_configured"
            elif last is None:
                state = "unavailable"
            elif now - last[0] > stale_after:
                state = "stale"
            elif last[1] != "ok":
                state = "offline"
            else:
                state = "degraded" if errors else "online"
            out.append(
                {
                    "service": service,
                    "state": state,
                    "source": "probe",
                    "probe_status": last[1] if last else None,
                    "latency_ms": last[2] if last else None,
                    "version": registry.safe_version(last[3]) if last else None,
                    "last_check_ms": last[0] if last else None,
                    "last_ok_ms": last_ok,
                    "recent_upstream_errors": errors,
                }
            )
        return out

    # --- system metrics -----------------------------------------------------------------------

    def latest_samples(self, conn, now: int, metrics) -> dict:
        interval = self.settings.system_sample_interval_seconds * 1000
        rows = conn.execute(
            f"SELECT metric, source, value, status, reason, ts_ms FROM system_samples WHERE ts_ms >= ? AND "
            "metric IN "
            f"({', '.join('?' * len(metrics))}) ORDER BY ts_ms",
            (now - max(10 * interval, 15 * MINUTE), *metrics),
        )
        latest = {}
        for metric, source, value, status, reason, ts in rows:
            latest[metric] = {
                "metric": metric,
                "source": source,
                "value": value,
                "status": status,
                "reason": reason or None,
                "measured_ms": ts,
            }
        result = {}
        for metric in metrics:
            item = latest.get(metric)
            if item is None:
                result[metric] = {
                    "metric": metric,
                    "source": None,
                    "value": None,
                    "status": "unavailable",
                    "reason": "no_sample",
                    "measured_ms": None,
                }
                continue
            if now - item["measured_ms"] > 3 * interval + 30_000:
                item = {**item, "status": "stale"}
            result[metric] = item
        return result

    def sample_series(self, conn, rng: Range, metrics) -> dict:
        retention = self.settings.retention
        if (
            rng.end - rng.start <= DAY + MINUTE
            and rng.start >= self._clock() - retention.system_raw_hours * HOUR
        ):
            # Samples arrive every few seconds to a minute: never bucket finer than one
            # minute, or most buckets would be empty and the line would fall apart.
            bucket = max(rng.bucket, MINUTE)
            first = _floor(rng.start, bucket)
            rng = Range(rng.key, rng.start, rng.end, rng.source, bucket, first)
            rows = conn.execute(
                f"SELECT metric, (ts_ms - ?) / ?, AVG(value), MIN(value), MAX(value) FROM system_samples "
                f"WHERE status = 'ok' AND ts_ms >= ? AND ts_ms < ? AND metric IN "
                f"({', '.join('?' * len(metrics))}) "
                "GROUP BY 1, 2",
                (rng.first_bucket, rng.bucket, rng.start, rng.end, *metrics),
            ).fetchall()
            source = "raw"
        else:
            resolution = MINUTE if rng.start >= self._clock() - retention.system_minute_days * DAY else HOUR
            bucket = max(rng.bucket, resolution)
            rng = Range(
                rng.key, rng.start, rng.end, rng.source, bucket, _floor(_floor(rng.start, resolution), bucket)
            )
            rows = conn.execute(
                f"SELECT metric, (bucket_ms - ?) / ?, SUM(sum) / SUM(n), MIN(min), MAX(max) FROM "
                "rollup_system "
                f"WHERE resolution_ms = ? AND bucket_ms >= ? AND bucket_ms < ? AND metric IN "
                f"({', '.join('?' * len(metrics))}) GROUP BY 1, 2",
                (rng.first_bucket, bucket, resolution, rng.first_bucket, rng.end, *metrics),
            ).fetchall()
            source = "minute" if resolution == MINUTE else "hour"
        series = {m: {} for m in metrics}
        for metric, b, avg, lo, hi in rows:
            series[metric][b] = {"avg": avg, "min": lo, "max": hi}
        return {
            "source": source,
            "bucket_ms": rng.bucket,
            "points": {
                m: [
                    {
                        "t": rng.first_bucket + i * rng.bucket,
                        **(series[m].get(i) or {"avg": None, "min": None, "max": None}),
                    }
                    for i in range(self._bucket_count(rng))
                ]
                for m in metrics
            },
        }

    # --- endpoints ----------------------------------------------------------------------------

    def overview(self, params: dict) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now)
        where, args = self._filters(params, raw=rng.source == "raw", allow=())

        def run(conn):
            effective = self._effective_range(conn, rng, where, args)
            w, a = self._filters(params, raw=effective.source == "raw", allow=())
            rows = self._request_buckets(conn, effective, w, a)
            summary = self._summarize(rows, 1)
            pct = self._percentiles(conn, effective, w, a, summary)
            coverage = self._coverage(conn, effective, now)
            series = self._series(rows, effective, coverage, self._bucket_p95(conn, effective, rows, w, a))
            client_rows = self._request_buckets(conn, effective, w, a, group_client=True)
            return {
                "range": effective.describe(),
                "totals": {k: v for k, v in summary.items() if not k.startswith("_")},
                "percentiles": pct,
                "series": series,
                "clients_series": self._client_series(client_rows, effective, coverage),
                "completeness": self.completeness(conn, effective.start, effective.end, now),
                "services": self.services(conn, now),
                "system": self.latest_samples(
                    conn,
                    now,
                    OVERVIEW_METRICS
                    + (
                        "disk.data.free_bytes",
                        "disk.data.total_bytes",
                        "process.rss_bytes",
                        "gpu.utilization_percent",
                        "gpu.memory_used_bytes",
                    ),
                ),
                "system_series": self.sample_series(
                    conn, effective, ("host.cpu.percent", "host.memory.used_percent")
                ),
            }

        data = self.query(run)
        data.update(
            self._build_info(now), active_requests=len(self.active(now)["items"]), ok=True, generated_ms=now
        )
        return data

    def _client_series(self, rows, rng: Range, coverage: dict) -> dict:
        totals: dict[str | None, int] = {}
        for row in rows:
            totals[row[1]] = totals.get(row[1], 0) + (row[2] or 0)
        named = sorted((c for c in totals if c is not None), key=lambda c: -totals[c])
        top = named[:TOP_CLIENTS]
        labels = (
            top
            + (["other"] if len(named) > TOP_CLIENTS else [])
            + ([UNAUTHENTICATED] if None in totals else [])
        )
        grid = {label: {} for label in labels}
        for row in rows:
            label = UNAUTHENTICATED if row[1] is None else (row[1] if row[1] in top else "other")
            grid[label][row[0]] = grid[label].get(row[0], 0) + (row[2] or 0)
        return {
            "labels": labels,
            "points": {
                label: [
                    grid[label].get(i, 0 if coverage.get(i) else None) for i in range(self._bucket_count(rng))
                ]
                for label in labels
            },
            "t": [rng.first_bucket + i * rng.bucket for i in range(self._bucket_count(rng))],
        }

    def _build_info(self, now: int) -> dict:
        build = getattr(self.monitor, "build", None)
        started = getattr(self.monitor, "started_ms", None)
        return {
            "build": {
                "version": VERSION,
                "commit": getattr(build, "commit", None),
                "image_tag": getattr(build, "image_tag", None),
                "source_state": getattr(build, "source_state", "unknown"),
                "local_patches": list(getattr(build, "local_patches", ()) or ()),
            },
            "uptime_ms": now - started if started else None,
        }

    def active(self, now: int, service: str | None = None) -> dict:
        items = []
        own = getattr(self.monitor, "instance_id", None)
        try:
            for rid, route, method, svc, started in self.monitor.recorder.active_snapshot(200):
                items.append(
                    {
                        "request_id": rid,
                        "route": route,
                        "method": method,
                        "service": svc,
                        "started_ms": started,
                        "age_ms": now - started,
                        "stale": False,
                    }
                )
        except Exception:
            pass

        def run(conn):
            stale_after = 3 * self.settings.flush_interval_seconds * 1000 + 5000
            return [
                {
                    "request_id": rid,
                    "route": route,
                    "method": method,
                    "service": svc,
                    "started_ms": started,
                    "age_ms": now - started,
                    "stale": now - snap > stale_after,
                }
                for rid, route, method, svc, started, snap in conn.execute(
                    "SELECT request_id, route, method, service, started_ms, snapshot_ms FROM live_requests "
                    "WHERE instance_id != ? ORDER BY started_ms LIMIT 200",
                    (own or "",),
                )
            ]

        try:
            items += self.query(run)
        except TelemetryUnavailable:
            pass
        if service:
            items = [i for i in items if i["service"] == service]
        return {"items": items[:200]}

    def activity(self, params: dict) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now, default="live")
        where, args = self._filters(params, raw=True, allow=("client", "service", "status", "route"))
        limit = min(max(_int(params.get("limit", 50), "limit"), 1), MAX_PAGE)
        cursor = params.get("cursor")
        cur_sql, cur_args = "", []
        if cursor:
            try:
                cts, cid = (int(x) for x in cursor.split(".", 1))
            except ValueError as exc:
                raise BadRequest("invalid_cursor") from exc
            cur_sql, cur_args = " AND (ts_ms < ? OR (ts_ms = ? AND id < ?))", [cts, cts, cid]
        if rng.start < now - self.settings.retention.raw_days * DAY:
            raise BadRequest("range_outside_raw_retention")
        rng = Range(rng.key, rng.start, rng.end, "raw", rng.bucket, rng.start)  # always raw rows

        def run(conn):
            rows = conn.execute(
                "SELECT id, request_id, ts_ms, client_id, route, method, service, status_code, error_code, "
                "duration_ms, ai_model, ai_calls, documents FROM request_events WHERE ts_ms >= ? AND ts_ms < "
                "?"
                f"{where}{cur_sql} ORDER BY ts_ms DESC, id DESC LIMIT ?",
                (rng.start, rng.end, *args, *cur_args, limit + 1),
            ).fetchall()
            page, more = rows[:limit], len(rows) > limit
            steps: dict[str, list] = {}
            ids = [r[1] for r in page]
            if ids:
                for rid, step, outcome, dur, load, pe, ev, tin, tout in conn.execute(
                    "SELECT request_id, step, outcome, duration_ms, ollama_load_ms, ollama_prompt_eval_ms, "
                    f"ollama_eval_ms, tokens_in, tokens_out FROM request_steps WHERE request_id IN "
                    f"({', '.join('?' * len(ids))}) ORDER BY request_id, seq",
                    ids,
                ):
                    steps.setdefault(rid, []).append(
                        {
                            "step": step,
                            "outcome": outcome,
                            "duration_ms": dur,
                            "ollama_load_ms": load,
                            "ollama_prompt_eval_ms": pe,
                            "ollama_eval_ms": ev,
                            "tokens_in": tin,
                            "tokens_out": tout,
                            "inclusive_of_parent": STEP_PARENTS.get(step),
                        }
                    )
            items = [
                {
                    "request_id": r[1],
                    "ts_ms": r[2],
                    "client_id": r[3],
                    "route": r[4],
                    "method": r[5],
                    "service": r[6],
                    "status_code": r[7],
                    "error_code": r[8],
                    "duration_ms": r[9],
                    "ai_model": r[10],
                    "ai_calls": r[11],
                    "documents": r[12],
                    "state": "finished",
                    "steps": steps.get(r[1], []),
                }
                for r in page
            ]
            last_flush = conn.execute("SELECT MAX(last_heartbeat_ms) FROM instances").fetchone()[0]
            return {
                "items": items,
                "next_cursor": f"{page[-1][2]}.{page[-1][0]}" if more and page else None,
                "data_until_ms": last_flush,
            }

        data = self.query(run)
        data.update(
            ok=True,
            generated_ms=now,
            range=rng.describe(),
            active=self.active(now, params.get("service"))["items"],
        )
        return data

    def clients(self, params: dict) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now, default="24h")
        self._filters(params, raw=True, allow=("service",))

        def run(conn):
            effective = self._effective_range(
                conn, rng, *self._filters(params, raw=rng.source == "raw", allow=("service",))
            )
            where, args = self._filters(params, raw=effective.source == "raw", allow=("service",))
            per_client = self._grouped(conn, effective, where, args, "COALESCE(NULLIF(client_id, ''), NULL)")
            per_service = self._grouped(
                conn, effective, where, args, "COALESCE(NULLIF(client_id, ''), NULL), service", key_columns=2
            )
            prev = self._previous(rng, now)
            previous = {}
            if prev is not None:
                pw, pa = self._filters(params, raw=prev.source == "raw", allow=("service",))
                for key, stats in self._grouped(
                    conn, prev, pw, pa, "COALESCE(NULLIF(client_id, ''), NULL)"
                ).items():
                    previous[key] = stats["count"]
            total = sum(s["count"] for s in per_client.values())
            clients = []
            for key, stats in sorted(per_client.items(), key=lambda kv: -kv[1]["count"]):
                services = {svc: s["count"] for (cid, svc), s in per_service.items() if cid == key}
                clients.append(
                    {
                        "client_id": key if key is not None else None,
                        "authenticated": key is not None,
                        **stats,
                        "share": _rate(stats["count"], total),
                        "services": services,
                        "previous_count": previous.get(key),
                        "previous_available": prev is not None,
                    }
                )
            coverage = self._coverage(conn, effective, now)
            rows = self._request_buckets(conn, effective, where, args, group_client=True)
            return {
                "range": effective.describe(),
                "previous_range": prev.describe() if prev else None,
                "total": total,
                "clients": clients,
                "series": self._client_series(rows, effective, coverage),
                "completeness": self.completeness(conn, effective.start, effective.end, now),
            }

        data = self.query(run)
        data.update(ok=True, generated_ms=now)
        return data

    def _previous(self, rng: Range, now: int) -> Range | None:
        span = rng.end - rng.start
        try:
            return self.parse_range({"range": "custom", "from": rng.start - span, "to": rng.start}, now)
        except BadRequest:
            return None

    def _grouped(self, conn, rng: Range, where: str, args: list, group: str, key_columns: int = 1) -> dict:
        ok = "CASE WHEN status_code BETWEEN 100 AND 399 THEN {n} ELSE 0 END"
        fail = "CASE WHEN status_code >= 400 OR status_code < 100 THEN {n} ELSE 0 END"
        if rng.source == "raw":
            rows = conn.execute(
                f"SELECT {group}, COUNT(*), SUM({ok.format(n=1)}), SUM({fail.format(n=1)}), "
                "SUM(duration_ms), "
                "SUM(documents), SUM(COALESCE(pages, 0)), SUM(pages IS NOT NULL), SUM(bytes_in), "
                "SUM(bytes_out), "
                "SUM(ai_calls), SUM(CASE WHEN tokens_in IS NOT NULL AND tokens_out IS NOT NULL THEN "
                "tokens_in ELSE 0 END), "
                "SUM(CASE WHEN tokens_in IS NOT NULL AND tokens_out IS NOT NULL THEN tokens_out ELSE 0 END), "
                f"MIN(duration_ms), MAX(duration_ms) FROM request_events WHERE ts_ms >= ? AND ts_ms < "
                f"?{where} "
                f"GROUP BY {', '.join(str(i + 1) for i in range(key_columns))}",
                (rng.start, rng.end, *args),
            ).fetchall()
            durations: dict = {}
            if key_columns == 1:
                for key, d in conn.execute(
                    f"SELECT {group}, duration_ms FROM request_events WHERE ts_ms >= ? "
                    f"AND ts_ms < ?{where} ORDER BY 2",
                    (rng.start, rng.end, *args),
                ):
                    durations.setdefault(key, []).append(d)
        else:
            hist = ", ".join(f"SUM({c})" for c in BUCKET_COLUMNS)
            rows = conn.execute(
                f"SELECT {group}, SUM(count), SUM({ok.format(n='count')}), SUM({fail.format(n='count')}), "
                "SUM(dur_sum), SUM(documents), SUM(pages), SUM(pages_known), SUM(bytes_in), SUM(bytes_out), "
                f"SUM(ai_calls), SUM(tokens_in), SUM(tokens_out), MIN(dur_min), MAX(dur_max), {hist} "
                "FROM rollup_requests WHERE resolution_ms = ? AND bucket_ms >= ? AND bucket_ms < ?"
                f"{where} GROUP BY {', '.join(str(i + 1) for i in range(key_columns))}",
                (rng.resolution, rng.first_bucket, rng.end, *args),
            ).fetchall()
        result = {}
        for row in rows:
            key = row[0] if key_columns == 1 else tuple(row[:key_columns])
            v = row[key_columns:]
            count = v[0] or 0
            if rng.source == "raw":
                p95 = exact_percentile(durations.get(key, []), 0.95) if key_columns == 1 else None
            else:
                p95 = histogram_percentile(list(v[14:]), 0.95, v[12], v[13])
            result[key] = {
                "count": count,
                "success": v[1] or 0,
                "failed": v[2] or 0,
                "success_rate": _rate(v[1], count),
                "failure_rate": _rate(v[2], count),
                "avg_ms": (v[3] or 0) / count if count else None,
                "p95_ms": p95,
                "p95_method": "exact" if rng.source == "raw" else "histogram",
                "documents": v[4] or 0,
                "pages": v[5] or 0,
                "pages_known": v[6] or 0,
                "bytes_in": v[7] or 0,
                "bytes_out": v[8] or 0,
                "ai_calls": v[9] or 0,
                "tokens_in": v[10] or 0,
                "tokens_out": v[11] or 0,
            }
        return result

    def performance(self, params: dict) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now, default="24h")
        allowed = ("client", "service", "route")
        self._filters(params, raw=True, allow=allowed)
        client_filter = bool(params.get("client"))
        raw_ok = rng.start >= now - self.settings.retention.raw_days * DAY

        def run(conn):
            use_raw = rng.source == "raw" or (client_filter and raw_ok)
            if client_filter and not use_raw:
                raise BadRequest("client_filter_requires_raw_retention")
            source_rng = (
                rng
                if not use_raw or rng.source == "raw"
                else Range(rng.key, rng.start, rng.end, "raw", rng.bucket, _floor(rng.start, rng.bucket))
            )
            where, args = self._filters(params, raw=True, allow=allowed)
            if use_raw:
                steps, series = self._raw_steps(conn, source_rng, where, args)
            else:
                steps, series = self._rollup_steps(conn, source_rng, params)
            req_where, req_args = self._filters(params, raw=source_rng.source == "raw", allow=allowed)
            rows = self._request_buckets(conn, source_rng, req_where, req_args)
            summary = self._summarize(rows, 1)
            return {
                "range": source_rng.describe(),
                "steps": steps,
                "step_series": series,
                "requests": {k: v for k, v in summary.items() if not k.startswith("_")}
                | {"percentiles": self._percentiles(conn, source_rng, req_where, req_args, summary)},
                "nesting": STEP_PARENTS,
                "completeness": self.completeness(conn, source_rng.start, source_rng.end, now),
            }

        data = self.query(run)
        data.update(ok=True, generated_ms=now)
        return data

    def _raw_steps(self, conn, rng: Range, where: str, args: list):
        prefixed = (
            where.replace("client_id", "e.client_id")
            .replace(" service", " e.service")
            .replace(" route", " e.route")
            .replace("status_code", "e.status_code")
        )
        rows = conn.execute(
            "SELECT s.step, s.outcome, s.duration_ms, s.ollama_load_ms, s.ollama_prompt_eval_ms, "
            "s.ollama_eval_ms, "
            "s.tokens_out, s.request_id, (s.ts_ms - ?) / ? FROM request_steps s JOIN request_events e "
            f"ON e.request_id = s.request_id WHERE s.ts_ms >= ? AND s.ts_ms < ?{prefixed} LIMIT ?",
            (rng.first_bucket, rng.bucket, rng.start, rng.end, *args, RAW_EXACT_LIMIT),
        ).fetchall()
        per_step: dict[str, dict] = {}
        children: dict[tuple, float] = {}
        for step, outcome, dur, load, pe, ev, tout, rid, b in rows:
            if step in STEP_PARENTS:
                key = (rid, STEP_PARENTS[step])
                children[key] = children.get(key, 0.0) + dur
        series: dict[str, dict[int, list]] = {}
        for step, outcome, dur, load, pe, ev, tout, rid, b in rows:
            s = per_step.setdefault(
                step,
                {
                    "durations": [],
                    "failed": 0,
                    "load": [],
                    "pe": [],
                    "ev": [],
                    "tok": 0,
                    "ev_known": 0.0,
                    "exclusive": [],
                },
            )
            s["durations"].append(dur)
            s["failed"] += outcome != "ok"
            for name, value in (("load", load), ("pe", pe), ("ev", ev)):
                if value is not None:
                    s[name].append(value)
            if tout is not None and ev:
                s["tok"] += tout
                s["ev_known"] += ev
            if (rid, step) in children:
                s["exclusive"].append(max(0.0, dur - children[(rid, step)]))
            series.setdefault(step, {}).setdefault(b, []).append(dur)
        steps = []
        for step, s in sorted(per_step.items()):
            d = sorted(s["durations"])
            steps.append(
                {
                    "step": step,
                    "count": len(d),
                    "failed": s["failed"],
                    "failure_rate": _rate(s["failed"], len(d)),
                    "avg_ms": sum(d) / len(d),
                    "min_ms": d[0],
                    "max_ms": d[-1],
                    **{f"{k}_ms": v for k, v in _pct(d).items()},
                    "method": "exact",
                    "duration_kind": "inclusive",
                    "exclusive_avg_ms": (sum(s["exclusive"]) / len(s["exclusive"]))
                    if s["exclusive"]
                    else None,
                    "parent": STEP_PARENTS.get(step),
                    "ollama": {
                        "load_avg_ms": _avg(s["load"]),
                        "prompt_eval_avg_ms": _avg(s["pe"]),
                        "eval_avg_ms": _avg(s["ev"]),
                        "reported_count": len(s["ev"]),
                        "eval_tokens_per_s": s["tok"] / (s["ev_known"] / 1000) if s["ev_known"] else None,
                    }
                    if step.startswith("ai.")
                    else None,
                }
            )
        points = {
            step: [_avg(by_b.get(i, [])) for i in range(self._bucket_count(rng))]
            for step, by_b in series.items()
        }
        return steps, {
            "t": [rng.first_bucket + i * rng.bucket for i in range(self._bucket_count(rng))],
            "avg_ms": points,
        }

    def _rollup_steps(self, conn, rng: Range, params: dict):
        where, args = "", []
        if params.get("route"):
            where, args = " AND route = ?", [params["route"]]
        elif params.get("service"):
            routes = [r for r, s in registry.SERVICE_BY_ROUTE.items() if s == params["service"]]
            if not routes:
                return [], {"t": [], "avg_ms": {}}
            where, args = f" AND route IN ({', '.join('?' * len(routes))})", routes
        hist = ", ".join(f"SUM({c})" for c in BUCKET_COLUMNS)
        rows = conn.execute(
            "SELECT step, SUM(count), SUM(CASE WHEN outcome != 'ok' THEN count ELSE 0 END), SUM(dur_sum), "
            "MIN(dur_min), MAX(dur_max), SUM(ollama_known), SUM(ollama_load_sum), "
            "SUM(ollama_prompt_eval_sum), "
            f"SUM(ollama_eval_sum), SUM(tokens_out), {hist} FROM rollup_steps WHERE resolution_ms = ? "
            f"AND bucket_ms >= ? AND bucket_ms < ?{where} GROUP BY step ORDER BY step",
            (rng.resolution, rng.first_bucket, rng.end, *args),
        ).fetchall()
        steps = []
        for row in rows:
            step, count, failed, dur_sum, lo, hi, known, load, pe, ev, tout = row[:11]
            pct = _hist_pct(list(row[11:]), lo, hi)
            steps.append(
                {
                    "step": step,
                    "count": count,
                    "failed": failed,
                    "failure_rate": _rate(failed, count),
                    "avg_ms": dur_sum / count if count else None,
                    "min_ms": lo,
                    "max_ms": hi,
                    **{f"{k}_ms": v for k, v in pct.items()},
                    "method": "histogram",
                    "duration_kind": "inclusive",
                    "exclusive_avg_ms": None,
                    "parent": STEP_PARENTS.get(step),
                    "ollama": {
                        "load_avg_ms": load / known if known else None,
                        "prompt_eval_avg_ms": pe / known if known else None,
                        "eval_avg_ms": ev / known if known else None,
                        "reported_count": known,
                        "eval_tokens_per_s": None,
                    }
                    if step.startswith("ai.")
                    else None,
                }
            )
        series_rows = conn.execute(
            "SELECT step, (bucket_ms - ?) / ?, SUM(dur_sum) / SUM(count) FROM rollup_steps WHERE "
            "resolution_ms = ? "
            f"AND bucket_ms >= ? AND bucket_ms < ?{where} GROUP BY 1, 2",
            (rng.first_bucket, rng.bucket, rng.resolution, rng.first_bucket, rng.end, *args),
        ).fetchall()
        grid: dict[str, dict] = {}
        for step, b, avg in series_rows:
            grid.setdefault(step, {})[b] = avg
        n = self._bucket_count(rng)
        return steps, {
            "t": [rng.first_bucket + i * rng.bucket for i in range(n)],
            "avg_ms": {s: [g.get(i) for i in range(n)] for s, g in grid.items()},
        }

    def server(self, params: dict) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now, default="1h")
        self._filters(params, raw=True, allow=())

        def run(conn):
            return {
                "range": rng.describe(),
                "latest": self.latest_samples(conn, now, SERVER_METRICS),
                "series": self.sample_series(conn, rng, SERVER_METRICS),
                "services": self.services(conn, now),
            }

        data = self.query(run)
        data.update(
            ok=True, generated_ms=now, sample_interval_ms=self.settings.system_sample_interval_seconds * 1000
        )
        return data

    def security(self, params: dict, *, username: str, current_session: str, idle_ms: int) -> dict:
        now = self._clock()
        rng = self.parse_range(params, now, default="24h")
        unknown = set(params) - {"range", "from", "to", "cursor", "limit", "category"}
        if unknown:
            raise BadRequest("unknown_parameter")
        category = params.get("category")
        if category and category not in set(AUDIT_CATEGORIES.values()):
            raise BadRequest("invalid_category")
        events = [e for e, c in AUDIT_CATEGORIES.items() if not category or c == category]
        limit = min(max(_int(params.get("limit", 50), "limit"), 1), MAX_PAGE)
        cursor = None
        if params.get("cursor"):
            try:
                cursor = tuple(int(x) for x in params["cursor"].split(".", 1))
            except ValueError as exc:
                raise BadRequest("invalid_cursor") from exc
            if len(cursor) != 2:
                raise BadRequest("invalid_cursor")
        data = self.auth.read_security(
            rng.start,
            rng.end,
            events=events,
            limit=limit,
            cursor=cursor,
            bucket_ms=rng.bucket,
            first_bucket=rng.first_bucket,
            username=username,
            current_session=current_session,
            now_ms=now,
            idle_ms=idle_ms,
        )
        n = self._bucket_count(rng)
        grid: dict[tuple, int] = {}
        for event, index, count in data.pop("timeline_rows"):
            key = (AUDIT_CATEGORIES.get(event, "other"), index)
            grid[key] = grid.get(key, 0) + count
        data["timeline"] = {
            "t": [rng.first_bucket + i * rng.bucket for i in range(n)],
            "categories": {
                cat: [grid.get((cat, i), 0) for i in range(n)]
                for cat in sorted(set(AUDIT_CATEGORIES.values()))
            },
        }
        data["counts_by_category"] = {}
        for event, n in data["counts"].items():
            cat = AUDIT_CATEGORIES.get(event, "other")
            data["counts_by_category"][cat] = data["counts_by_category"].get(cat, 0) + n
        data.update(
            ok=True,
            generated_ms=now,
            range={**rng.describe(), "source": "audit", "approximate": False},
            categories=AUDIT_CATEGORIES,
        )
        return data

    def meta(self, *, poll: dict, web_settings) -> dict:
        now = self._clock()

        def run(conn):
            first = {
                name: conn.execute(sql).fetchone()[0]
                for name, sql in (
                    ("raw", "SELECT MIN(ts_ms) FROM request_events"),
                    ("minute", "SELECT MIN(bucket_ms) FROM rollup_requests WHERE resolution_ms = 60000"),
                    ("hour", "SELECT MIN(bucket_ms) FROM rollup_requests WHERE resolution_ms = 3600000"),
                )
            }
            return {"first_data_ms": first}

        try:
            availability = self.query(run)
        except TelemetryUnavailable:
            availability = {"first_data_ms": None, "telemetry_available": False}
        r = self.settings.retention
        return {
            "ok": True,
            "generated_ms": now,
            "poll_seconds": poll,
            **self._build_info(now),
            **availability,
            "retention": {
                "raw_days": r.raw_days,
                "minute_rollup_days": r.minute_rollup_days,
                "hour_rollup_days": r.hour_rollup_days,
                "system_raw_hours": r.system_raw_hours,
                "audit_days": web_settings.audit_days,
            },
            "ranges": list(RANGES) + ["custom"],
            "max_points": MAX_POINTS,
            "page_limit": MAX_PAGE,
            "clients": sorted(self.known_clients) + [UNAUTHENTICATED],
            "services": sorted(registry.SERVICES - {"none"}),
            "routes": sorted(registry.SERVICE_BY_ROUTE),
            "sections": {
                "overview": True,
                "activity": True,
                "clients": True,
                "performance": True,
                "server": True,
                "security": True,
                "ollama": "M5",
                "stirling": "M5",
                "logs": "M6",
                "connectors": "#2",
            },
        }


def _avg(values: list) -> float | None:
    return sum(values) / len(values) if values else None
