"""Ollama and Stirling PDF views of the dashboard (Phase M5).

Built on the M4 query layer (Dashboard: read-only connection, time budget,
range/source selection, coverage and completeness) - no second monitoring
implementation. Everything is read from data the collector already stored:

- DocPipe-measured AI and Stirling calls: request_steps (raw) or rollup_steps
  (minute/hour), never mixed in one answer;
- Ollama-reported timings and tokens: the same step rows (labelled "ollama");
- probes and model snapshots: service_probes, ollama_models;
- running AI calls: the recorder and live_ai_steps snapshots.

No browser request contacts Ollama or Stirling.

Throughput (tokens/s) is weighted: sum(valid tokens) / sum(their measured
seconds), from pairs where both the token count and its duration (> 0) are
known. It is the speed while Ollama was working on DocPipe's calls - not the
server's throughput over wall-clock time including idle periods. Rollup buckets
written before schema v3 have no pair sums (NULL); answers then say the
throughput is partial or not available instead of treating them as zero.
"""

from __future__ import annotations

import ipaddress
from collections import Counter
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from system.monitoring import registry
from system.monitoring.stats import BUCKET_COLUMNS, PERCENTILES, exact_percentile, histogram_percentile
from system.monitoring.web.dashboard import (
    DAY,
    MINUTE,
    RAW_EXACT_LIMIT,
    UNAUTHENTICATED,
    BadRequest,
    Range,
    _floor,
    _rate,
)

AI_STEPS = ("ai.generate", "ai.warmup")
STIRLING_STEPS = tuple(registry.STIRLING_OPERATION_BY_STEP)
MODEL_SNAPSHOT_LOOKBACK = 7 * DAY
UNREGISTERED = registry.UNREGISTERED
AI_ERROR_OUTCOMES = ("ai_timeout", "ai_unavailable", "ai_invalid_response", "ai_processing_failed", "error")
PROBE_FAILURES = ("timeout", "connection_error", "http_error", "invalid_response")


@dataclass
class Agg:
    count: int = 0
    success: int = 0
    durations: list = field(default_factory=list)
    hist: list = field(default_factory=lambda: [0] * len(BUCKET_COLUMNS))
    dur_sum: float = 0.0
    dur_min: float | None = None
    dur_max: float | None = None
    ollama_known: int = 0
    load_sum: float = 0.0
    prompt_eval_sum: float = 0.0
    eval_sum: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_known: int = 0
    eval_pair_tokens: int = 0
    eval_pair_ms: float = 0.0
    eval_pairs: int = 0
    prompt_pair_tokens: int = 0
    prompt_pair_ms: float = 0.0
    prompt_pairs: int = 0
    pair_rows_missing: int = 0  # rollup rows without pair sums (written before v3)
    outcomes: Counter = field(default_factory=Counter)

    def add_raw(self, outcome, dur, load, pe, ev, tin, tout) -> None:
        self.count += 1
        self.success += outcome == "ok"
        self.outcomes[outcome] += 1
        self.durations.append(dur)
        self.dur_sum += dur
        self.dur_min = dur if self.dur_min is None else min(self.dur_min, dur)
        self.dur_max = dur if self.dur_max is None else max(self.dur_max, dur)
        if load is not None and pe is not None and ev is not None:
            self.ollama_known += 1
            self.load_sum += load
            self.prompt_eval_sum += pe
            self.eval_sum += ev
        if tin is not None and tout is not None:
            self.tokens_in += tin
            self.tokens_out += tout
            self.tokens_known += 1
        if tout is not None and ev is not None and ev > 0:
            self.eval_pair_tokens += tout
            self.eval_pair_ms += ev
            self.eval_pairs += 1
        if tin is not None and pe is not None and pe > 0:
            self.prompt_pair_tokens += tin
            self.prompt_pair_ms += pe
            self.prompt_pairs += 1

    def add_rollup(self, row: dict) -> None:
        n = row["count"]
        self.count += n
        self.success += n if row["outcome"] == "ok" else 0
        self.outcomes[row["outcome"]] += n
        self.dur_sum += row["dur_sum"]
        self.dur_min = row["dur_min"] if self.dur_min is None else min(self.dur_min, row["dur_min"])
        self.dur_max = row["dur_max"] if self.dur_max is None else max(self.dur_max, row["dur_max"])
        for i, c in enumerate(row["hist"]):
            self.hist[i] += c
        self.ollama_known += row["ollama_known"]
        self.load_sum += row["load_sum"]
        self.prompt_eval_sum += row["pe_sum"]
        self.eval_sum += row["ev_sum"]
        self.tokens_in += row["tokens_in"]
        self.tokens_out += row["tokens_out"]
        self.tokens_known += row["tokens_known"]
        if row["eval_pairs"] is None:
            self.pair_rows_missing += n
        else:
            self.eval_pair_tokens += row["eval_pair_tokens"]
            self.eval_pair_ms += row["eval_pair_ms"]
            self.eval_pairs += row["eval_pairs"]
            self.prompt_pair_tokens += row["prompt_pair_tokens"]
            self.prompt_pair_ms += row["prompt_pair_ms"]
            self.prompt_pairs += row["prompt_pairs"]

    def result(self, exact: bool, ai: bool = False) -> dict:
        failed = self.count - self.success
        if exact:
            ordered = sorted(self.durations)
            pct = {k: exact_percentile(ordered, p) for k, p in PERCENTILES.items()}
        else:
            pct = {
                k: histogram_percentile(self.hist, p, self.dur_min, self.dur_max)
                for k, p in PERCENTILES.items()
            }
        out = {
            "count": self.count,
            "success": self.success,
            "failed": failed,
            "success_rate": _rate(self.success, self.count),
            "failure_rate": _rate(failed, self.count),
            "avg_ms": self.dur_sum / self.count if self.count else None,
            "min_ms": self.dur_min,
            "max_ms": self.dur_max,
            **{f"{k}_ms": v for k, v in pct.items()},
            "percentile_method": "exact" if exact else "histogram",
            "outcomes": {k: v for k, v in self.outcomes.items() if k != "ok"},
        }
        if ai:
            known = self.ollama_known
            out["ollama"] = {
                "reported_count": known,
                "load_avg_ms": self.load_sum / known if known else None,
                "prompt_eval_avg_ms": self.prompt_eval_sum / known if known else None,
                "eval_avg_ms": self.eval_sum / known if known else None,
            }
            out["tokens"] = {
                "input": self.tokens_in,
                "output": self.tokens_out,
                "known_calls": self.tokens_known,
            }
            out["throughput"] = {
                "generation_tokens_per_s": self.eval_pair_tokens / (self.eval_pair_ms / 1000)
                if self.eval_pair_ms > 0
                else None,
                "prompt_tokens_per_s": self.prompt_pair_tokens / (self.prompt_pair_ms / 1000)
                if self.prompt_pair_ms > 0
                else None,
                "generation_pairs": self.eval_pairs,
                "prompt_pairs": self.prompt_pairs,
                "calls_without_pair_data": self.pair_rows_missing,
                "partial": self.pair_rows_missing > 0,
                "weighting": "sum(tokens) / sum(seconds) of valid pairs",
            }
        return out


def _slot(groups: dict, key) -> Agg:
    """groups[key], created on first use (dict.setdefault would build an Agg per row)."""
    agg = groups.get(key)
    if agg is None:
        agg = groups[key] = Agg()
    return agg


def _ollama_location(base_url: str) -> str:
    """'local' (same host as DocPipe), 'remote' or 'unknown' - never the URL."""
    try:
        host = (urlsplit(base_url).hostname or "").strip("[]").lower()
    except ValueError:
        return "unknown"
    if not host:
        return "unknown"
    if host == "localhost":
        return "local"
    try:
        return "local" if ipaddress.ip_address(host).is_loopback else "remote"
    except ValueError:
        return "remote"


class UpstreamViews:
    def __init__(self, dashboard, *, ollama_base_url: str, known_models) -> None:
        self.d = dashboard
        self.ollama_location = _ollama_location(ollama_base_url)
        self.known_models = frozenset(known_models)

    # --- parameters ----------------------------------------------------------------

    def _validate(self, params: dict, allowed: tuple[str, ...]) -> None:
        unknown = set(params) - {"range", "from", "to", *allowed}
        if unknown:
            raise BadRequest("unknown_parameter")
        client = params.get("client")
        if client and client != UNAUTHENTICATED and client not in self.d.known_clients:
            raise BadRequest("invalid_client")
        status = params.get("status")
        if status and status not in ("success", "failed"):
            raise BadRequest("invalid_status")
        model = params.get("model")
        if model and model not in self.known_models and model != UNREGISTERED:
            raise BadRequest("invalid_model")

    def _source(self, conn, rng: Range, now: int, steps, where: str, args: list, client_filter: bool):
        """Same rule as the M4 views: the range's own source (raw <= 1 h, minute
        <= 24 h, hour beyond); raw falls back to minute rollups above
        RAW_EXACT_LIMIT rows. A client filter needs raw rows - as in the M4
        performance view it then stays raw, bounded by the query budget."""
        raw_ok = rng.start >= now - self.d.settings.retention.raw_days * DAY
        if raw_ok and client_filter:
            return "raw"
        if rng.source != "raw":
            if client_filter:
                raise BadRequest("client_filter_requires_raw_retention")
            return rng.source
        if raw_ok:
            count = conn.execute(
                "SELECT COUNT(*) FROM request_steps s LEFT JOIN request_events e ON e.request_id = "
                "s.request_id "
                f"WHERE s.step IN ({', '.join('?' * len(steps))}) AND s.ts_ms >= ? AND s.ts_ms < ?{where}",
                (*steps, rng.start, rng.end, *args),
            ).fetchone()[0]
            if count <= RAW_EXACT_LIMIT:
                return "raw"
        if client_filter:
            raise BadRequest("client_filter_requires_raw_retention")
        return rng.source if rng.source != "raw" else "minute"

    @staticmethod
    def _rollup_range(rng: Range, source: str) -> Range:
        resolution = MINUTE if source == "minute" else 3_600_000
        bucket = max(rng.bucket, resolution)
        return Range(
            rng.key, rng.start, rng.end, source, bucket, _floor(_floor(rng.start, resolution), bucket)
        )

    @staticmethod
    def _raw_range(rng: Range) -> Range:
        return Range(rng.key, rng.start, rng.end, "raw", rng.bucket, _floor(rng.start, rng.bucket))

    def _step_rows(
        self,
        conn,
        rng: Range,
        source: str,
        steps,
        raw_where: str,
        raw_args: list,
        roll_where: str,
        roll_args: list,
    ):
        """Normalized step rows of ONE source: dicts with bucket index and the
        dimensions; raw rows carry single values, rollup rows sums."""
        if source == "raw":
            rows = conn.execute(
                "SELECT s.ts_ms, s.step, s.outcome, s.duration_ms, s.ollama_load_ms, "
                "s.ollama_prompt_eval_ms, "
                "s.ollama_eval_ms, s.tokens_in, s.tokens_out, s.ai_model, s.route, e.client_id "
                "FROM request_steps s LEFT JOIN request_events e ON e.request_id = s.request_id "
                f"WHERE s.step IN ({', '.join('?' * len(steps))}) AND s.ts_ms >= ? AND s.ts_ms < "
                f"?{raw_where}",
                (*steps, rng.start, rng.end, *raw_args),
            ).fetchall()
            return [
                {
                    "b": (r[0] - rng.first_bucket) // rng.bucket,
                    "step": r[1],
                    "outcome": r[2],
                    "dur": r[3],
                    "load": r[4],
                    "pe": r[5],
                    "ev": r[6],
                    "tin": r[7],
                    "tout": r[8],
                    "model": r[9],
                    "route": r[10],
                    "client": r[11],
                }
                for r in rows
            ]
        resolution = MINUTE if source == "minute" else 3_600_000
        hist = ", ".join(BUCKET_COLUMNS)
        rows = conn.execute(
            "SELECT bucket_ms, step, outcome, route, ai_model, count, dur_sum, dur_min, dur_max, "
            "ollama_known, "
            "ollama_load_sum, ollama_prompt_eval_sum, ollama_eval_sum, tokens_in, tokens_out, tokens_known, "
            "eval_pair_tokens, eval_pair_ms, eval_pairs, prompt_pair_tokens, prompt_pair_ms, prompt_pairs, "
            f"{hist} FROM rollup_steps WHERE resolution_ms = ? AND bucket_ms >= ? AND bucket_ms < ? "
            f"AND step IN ({', '.join('?' * len(steps))}){roll_where}",
            (resolution, rng.first_bucket, rng.end, *steps, *roll_args),
        ).fetchall()
        out = []
        for r in rows:
            out.append(
                {
                    "b": (r[0] - rng.first_bucket) // rng.bucket,
                    "step": r[1],
                    "outcome": r[2],
                    "route": r[3],
                    "model": r[4] or None,
                    "client": None,
                    "count": r[5],
                    "dur_sum": r[6],
                    "dur_min": r[7],
                    "dur_max": r[8],
                    "ollama_known": r[9],
                    "load_sum": r[10],
                    "pe_sum": r[11],
                    "ev_sum": r[12],
                    "tokens_in": r[13],
                    "tokens_out": r[14],
                    "tokens_known": r[15],
                    "eval_pair_tokens": r[16],
                    "eval_pair_ms": r[17],
                    "eval_pairs": r[18],
                    "prompt_pair_tokens": r[19],
                    "prompt_pair_ms": r[20],
                    "prompt_pairs": r[21],
                    "hist": list(r[22:]),
                }
            )
        return out

    @staticmethod
    def _add(agg: Agg, row: dict, source: str) -> None:
        if source == "raw":
            agg.add_raw(
                row["outcome"], row["dur"], row["load"], row["pe"], row["ev"], row["tin"], row["tout"]
            )
        else:
            agg.add_rollup(row)

    def _probe_history(self, conn, service: str, rng: Range) -> dict:
        bucket = max(rng.bucket, MINUTE)
        first = _floor(rng.start, bucket)
        rows = conn.execute(
            "SELECT (ts_ms - ?) / ?, status, COUNT(*), AVG(latency_ms) FROM service_probes WHERE service = ? "
            "AND ts_ms >= ? AND ts_ms < ? GROUP BY 1, 2",
            (first, bucket, service, rng.start, rng.end),
        ).fetchall()
        n = max(1, -(-(rng.end - first) // bucket))
        ok, failed, latency = [None] * n, [None] * n, [None] * n
        status_counts: Counter = Counter()
        for b, status, count, avg in rows:
            if not 0 <= b < n:
                continue
            status_counts[status] += count
            if status == "ok":
                ok[b] = (ok[b] or 0) + count
                latency[b] = avg
            elif status != "not_configured":
                failed[b] = (failed[b] or 0) + count
        for i in range(n):  # a probed bucket gets 0 for the other kind; unprobed stays null
            if ok[i] is not None or failed[i] is not None:
                ok[i], failed[i] = ok[i] or 0, failed[i] or 0
        recent = conn.execute(
            "SELECT ts_ms, status, latency_ms, version FROM service_probes WHERE service = ? AND ts_ms < ? "
            "ORDER BY ts_ms DESC LIMIT 20",
            (service, rng.end),
        ).fetchall()
        return {
            "t": [first + i * bucket for i in range(n)],
            "bucket_ms": bucket,
            "ok": ok,
            "failed": failed,
            "latency_ms": latency,
            "status_counts": dict(status_counts),
            "failures": {k: status_counts.get(k, 0) for k in PROBE_FAILURES},
            "recent": [
                {"ts_ms": t, "status": s, "latency_ms": lat, "version": registry.safe_version(v)}
                for t, s, lat, v in recent
            ],
        }

    def _health(self, conn, service: str, now: int) -> dict:
        return next(s for s in self.d.services(conn, now) if s["service"] == service)

    # --- Ollama ------------------------------------------------------------------------

    def _models(self, conn, now: int) -> dict:
        latest = conn.execute(
            "SELECT MAX(ts_ms) FROM ollama_models WHERE ts_ms >= ?", (now - MODEL_SNAPSHOT_LOOKBACK,)
        ).fetchone()[0]
        stale_after = 3 * self.d.settings.probe_interval_seconds * 1000 + 30_000
        if latest is None:
            return {
                "measured_ms": None,
                "stale": None,
                "installed": [],
                "loaded": [],
                "reported_vram_bytes": None,
                "source": "ollama",
            }
        rows = conn.execute(
            "SELECT name, installed, loaded, size_bytes, size_vram_bytes, family, parameter_size, "
            "quantization, "
            "context_length, expires_ms FROM ollama_models WHERE ts_ms = ? ORDER BY loaded DESC, "
            "name LIMIT 200",
            (latest,),
        ).fetchall()
        models = []
        for name, installed, loaded, size, vram, family, params, quant, ctx, expires in rows:
            models.append(
                {
                    "name": registry.safe_id(name) or registry.INVALID,
                    "installed": bool(installed),
                    "loaded": bool(loaded),
                    "size_bytes": size,
                    "size_vram_bytes": vram if loaded else None,
                    "family": registry.safe_id(family),
                    "parameter_size": registry.safe_id(params),
                    "quantization": registry.safe_id(quant),
                    "context_length": ctx,
                    # only a future expiry relative to the measurement is meaningful
                    "expires_ms": expires if loaded and expires and expires > latest else None,
                }
            )
        loaded_vram = [
            m["size_vram_bytes"] for m in models if m["loaded"] and m["size_vram_bytes"] is not None
        ]
        return {
            "measured_ms": latest,
            "stale": now - latest > stale_after,
            "source": "ollama",
            "installed": [m for m in models if m["installed"]],
            "loaded": [m for m in models if m["loaded"]],
            "reported_vram_bytes": sum(loaded_vram) if loaded_vram else None,
        }

    def _active_ai(self, conn, now: int, params: dict) -> list[dict]:
        items = []
        own = getattr(self.d.monitor, "instance_id", "")
        try:
            for rid, client, model, op, step, started in self.d.monitor.recorder.ai_snapshot(200):
                items.append(
                    {
                        "request_id": rid,
                        "client_id": client,
                        "model": model,
                        "operation": op,
                        "step": step,
                        "started_ms": started,
                        "age_ms": now - started,
                        "stale": False,
                        "state": "running",
                    }
                )
        except Exception:
            pass
        stale_after = 3 * self.d.settings.flush_interval_seconds * 1000 + 5000
        for rid, client, model, op, step, started, snap in conn.execute(
            "SELECT request_id, client_id, ai_model, operation, step, started_ms, snapshot_ms FROM "
            "live_ai_steps "
            "WHERE instance_id != ? ORDER BY started_ms LIMIT 200",
            (own,),
        ):
            items.append(
                {
                    "request_id": rid,
                    "client_id": client if client in self.d.known_clients else None,
                    "model": registry.safe_id(model) or registry.INVALID,
                    "operation": registry.member(op, registry.AI_OPERATIONS, "other"),
                    "step": registry.member(step, registry.STEPS, registry.INVALID),
                    "started_ms": started,
                    "age_ms": now - started,
                    "stale": now - snap > stale_after,
                    "state": "running" if now - snap <= stale_after else "unknown",
                }
            )
        if params.get("model"):
            items = [i for i in items if i["model"] == params["model"]]
        if params.get("client"):
            want = None if params["client"] == UNAUTHENTICATED else params["client"]
            items = [i for i in items if i["client_id"] == want]
        return items[:200]

    def ollama(self, params: dict) -> dict:
        now = self.d._clock()
        rng = self.d.parse_range(params, now, default="24h")
        self._validate(params, ("model", "client", "operation", "status"))
        operation = params.get("operation")
        if operation and operation not in registry.AI_OPERATIONS:
            raise BadRequest("invalid_operation")

        raw_where, raw_args, roll_where, roll_args = "", [], "", []
        if params.get("model"):
            model = params["model"]
            raw_where += " AND COALESCE(s.ai_model, '') = ?"
            raw_args.append(model)
            roll_where += " AND ai_model = ?"
            roll_args.append(model)
        if params.get("status"):
            cond = "= 'ok'" if params["status"] == "success" else "!= 'ok'"
            raw_where += f" AND s.outcome {cond}"
            roll_where += f" AND outcome {cond}"
        if params.get("client"):
            if params["client"] == UNAUTHENTICATED:
                raw_where += " AND e.client_id IS NULL"
            else:
                raw_where += " AND e.client_id = ?"
                raw_args.append(params["client"])
        if operation:
            if operation == "warmup":
                raw_where += " AND s.step = 'ai.warmup'"
                roll_where += " AND step = 'ai.warmup'"
            else:
                routes = [r for r, op in registry.AI_OPERATION_BY_ROUTE.items() if op == operation]
                marks = ", ".join("?" * len(routes)) or "NULL"
                if operation == "other":
                    known = list(registry.AI_OPERATION_BY_ROUTE)
                    marks = ", ".join("?" * len(known))
                    raw_where += f" AND s.step = 'ai.generate' AND s.route NOT IN ({marks})"
                    roll_where += f" AND step = 'ai.generate' AND route NOT IN ({marks})"
                    raw_args += known
                    roll_args += known
                else:
                    raw_where += f" AND s.step = 'ai.generate' AND s.route IN ({marks})"
                    roll_where += f" AND step = 'ai.generate' AND route IN ({marks})"
                    raw_args += routes
                    roll_args += routes

        def run(conn):
            source = self._source(conn, rng, now, AI_STEPS, raw_where, raw_args, bool(params.get("client")))
            eff = self._raw_range(rng) if source == "raw" else self._rollup_range(rng, source)
            rows = self._step_rows(conn, eff, source, AI_STEPS, raw_where, raw_args, roll_where, roll_args)
            n = self.d._bucket_count(eff)
            exact = source == "raw"
            calls, warmups = Agg(), Agg()
            by_model: dict[str, Agg] = {}
            by_client: dict[str | None, Agg] = {}
            by_operation: dict[str, Agg] = {}
            buckets = [Agg() for _ in range(n)]
            for row in rows:
                op = registry.ai_operation(row["route"], row["step"])
                if row["step"] == "ai.warmup":
                    self._add(warmups, row, source)
                    continue
                self._add(calls, row, source)
                self._add(_slot(by_model, row["model"] or UNREGISTERED), row, source)
                self._add(_slot(by_operation, op), row, source)
                if source == "raw":
                    self._add(_slot(by_client, row["client"]), row, source)
                if 0 <= row["b"] < n:
                    self._add(buckets[row["b"]], row, source)
            coverage = self.d._coverage(conn, eff, now)
            series = []
            for i, agg in enumerate(buckets):
                cov = coverage.get(i)
                empty = agg.count == 0
                zero = 0 if cov else None
                res = agg.result(exact, ai=True) if not empty else None
                series.append(
                    {
                        "t": eff.first_bucket + i * eff.bucket,
                        "coverage": cov,
                        "success": agg.success if not empty else zero,
                        "failed": agg.count - agg.success if not empty else zero,
                        "avg_ms": res["avg_ms"] if res else None,
                        "p95_ms": res["p95_ms"] if res else None,
                        "tokens_in": agg.tokens_in if not empty else zero,
                        "tokens_out": agg.tokens_out if not empty else zero,
                        "load_avg_ms": res["ollama"]["load_avg_ms"] if res else None,
                        "generation_tps": res["throughput"]["generation_tokens_per_s"] if res else None,
                        "prompt_tps": res["throughput"]["prompt_tokens_per_s"] if res else None,
                        "errors": res["outcomes"] if res else ({} if cov else None),
                    }
                )
            return {
                "range": eff.describe(),
                "health": self._health(conn, "ollama", now),
                "probes": self._probe_history(conn, "ollama", eff),
                "models": self._models(conn, now),
                "active": self._active_ai(conn, now, params),
                "calls": calls.result(exact, ai=True),
                "warmups": warmups.result(exact, ai=True),
                "by_model": {k: v.result(exact, ai=True) for k, v in sorted(by_model.items())},
                "by_operation": {k: v.result(exact, ai=True) for k, v in sorted(by_operation.items())},
                "by_client": [
                    {"client_id": k, **v.result(exact, ai=True)}
                    for k, v in sorted(by_client.items(), key=lambda kv: -kv[1].count)
                ]
                if source == "raw"
                else None,
                "series": series,
                "completeness": self.d.completeness(conn, eff.start, eff.end, now),
                "gpu": {
                    "docpipe_host": self.d.latest_samples(
                        conn,
                        now,
                        (
                            "gpu.utilization_percent",
                            "gpu.memory_used_bytes",
                            "gpu.memory_total_bytes",
                            "gpu.temperature_celsius",
                        ),
                    ),
                    "ollama_location": self.ollama_location,
                },
            }

        data = self.d.query(run)
        data.update(
            ok=True,
            generated_ms=now,
            scope="docpipe_observed",
            known_models=sorted(self.known_models),
            operations=sorted(registry.AI_OPERATIONS),
        )
        return data

    # --- Stirling ------------------------------------------------------------------------

    def stirling(self, params: dict) -> dict:
        now = self.d._clock()
        rng = self.d.parse_range(params, now, default="24h")
        self._validate(params, ("client", "operation", "status"))
        steps = STIRLING_STEPS
        operation = params.get("operation")
        if operation:
            step = next((s for s, op in registry.STIRLING_OPERATION_BY_STEP.items() if op == operation), None)
            if step is None:
                raise BadRequest("invalid_operation")
            steps = (step,)
        raw_where, raw_args, roll_where = "", [], ""
        if params.get("status"):
            cond = "= 'ok'" if params["status"] == "success" else "!= 'ok'"
            raw_where += f" AND s.outcome {cond}"
            roll_where += f" AND outcome {cond}"
        if params.get("client"):
            if params["client"] == UNAUTHENTICATED:
                raw_where += " AND e.client_id IS NULL"
            else:
                raw_where += " AND e.client_id = ?"
                raw_args.append(params["client"])

        def run(conn):
            source = self._source(conn, rng, now, steps, raw_where, raw_args, bool(params.get("client")))
            eff = self._raw_range(rng) if source == "raw" else self._rollup_range(rng, source)
            rows = self._step_rows(conn, eff, source, steps, raw_where, raw_args, roll_where, [])
            n = self.d._bucket_count(eff)
            exact = source == "raw"
            total = Agg()
            by_op: dict[str, Agg] = {}
            by_route: dict[str, Agg] = {}
            by_client: dict[str | None, Agg] = {}
            per_bucket: dict[str, list[Agg]] = {}
            for row in rows:
                op = registry.STIRLING_OPERATION_BY_STEP[row["step"]]
                self._add(total, row, source)
                self._add(_slot(by_op, op), row, source)
                self._add(_slot(by_route, row["route"]), row, source)
                if source == "raw":
                    self._add(_slot(by_client, row["client"]), row, source)
                if 0 <= row["b"] < n:
                    if op not in per_bucket:  # not setdefault: its default would be built for every row
                        per_bucket[op] = [Agg() for _ in range(n)]
                    self._add(per_bucket[op][row["b"]], row, source)
            coverage = self.d._coverage(conn, eff, now)
            t = [eff.first_bucket + i * eff.bucket for i in range(n)]
            series = {}
            for op, buckets in per_bucket.items():
                series[op] = {
                    "count": [
                        b.count if b.count else (0 if coverage.get(i) else None)
                        for i, b in enumerate(buckets)
                    ],
                    "failed": [
                        (b.count - b.success) if b.count else (0 if coverage.get(i) else None)
                        for i, b in enumerate(buckets)
                    ],
                    "avg_ms": [b.dur_sum / b.count if b.count else None for b in buckets],
                }
            return {
                "range": eff.describe(),
                "health": self._health(conn, "stirling", now),
                "probes": self._probe_history(conn, "stirling", eff),
                "total": total.result(exact),
                "by_operation": {k: v.result(exact) for k, v in sorted(by_op.items())},
                "by_route": {k: v.result(exact) for k, v in sorted(by_route.items())},
                "by_client": [
                    {"client_id": k, **v.result(exact)}
                    for k, v in sorted(by_client.items(), key=lambda kv: -kv[1].count)
                ]
                if source == "raw"
                else None,
                "series": {"t": t, "coverage": [coverage.get(i) for i in range(n)], "operations": series},
                "completeness": self.d.completeness(conn, eff.start, eff.end, now),
            }

        data = self.d.query(run)
        data.update(
            ok=True,
            generated_ms=now,
            scope="docpipe_observed",
            operations=sorted(registry.STIRLING_OPERATION_BY_STEP.values()),
        )
        return data
