"""Condense loadtest results ($WORKROOT/results) into Markdown tables.

python3 loadtest/summarize.py /tmp/dpm7/results > summary.md
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/dpm7/results")
ROUTES = ("/api/v1/health", "/api/v1/documents/extract-text", "/api/v1/documents/analyze")


def load(run: str, name: str):
    path = ROOT / run / name
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def rss_mib(proc) -> float | None:
    if not proc:
        return None
    return round(sum(p["rss_kib"] for p in proc["processes"]) / 1024, 1)


def mean(values):
    values = [v for v in values if v is not None]
    return round(statistics.fmean(values), 2) if values else None


def completeness(run: str) -> str:
    lg, db = load(run, "load.json"), load(run, "db.json")
    if not lg or not db or "request_events" not in db:
        return "–"
    persisted = sum(db.get("by_route", {}).get(r, 0) for r in ROUTES)
    dq = db.get("data_quality", {})
    dropped = (dq.get("requests_dropped_buffer") or 0) + (dq.get("requests_dropped_store") or 0)
    sent = lg.get("sent_total", 0)
    return f"{persisted} + {dropped} dropped / {sent} sent"


def overhead() -> None:
    print("## Overhead (sequential, 1 worker, 3 repetitions each)\n")
    print(
        "| Variant | Idle CPU % (1 core) | RSS idle MiB | health p50 / p95 / p99 ms "
        "| extract p50 / p95 / p99 ms |"
    )
    print("|---|---|---|---|---|")
    for v in ("off", "collect", "web"):
        runs = [f"overhead-{v}-r{i}" for i in (1, 2, 3)]
        idle = mean([(load(r, "idle.json") or {}).get("cpu_percent_of_one_core") for r in runs])
        rss = mean([rss_mib(load(r, "proc_idle.json")) for r in runs])
        cells = []
        for kind in ("health", "extract"):
            stats = [(load(r, f"load_{kind}.json") or {}).get("api", {}) for r in runs]
            cells.append(
                " / ".join(str(mean([s.get(k) for s in stats])) for k in ("p50_ms", "p95_ms", "p99_ms"))
            )
        print(f"| {v} | {idle} | {rss} | {cells[0]} | {cells[1]} |")
    print()


def matrix() -> None:
    print("## 50 concurrent requests, 60 s\n")
    print(
        "| Run | rps | p50 / p95 / p99 ms | non-200 | dashboard p95 ms | RSS after MiB | DB+WAL MiB | "
        "completeness | flush failures |"
    )
    print("|---|---|---|---|---|---|---|---|---|")
    runs = [f"matrix-{v}-w{w}-r{r}" for w in (1, 4) for v in ("off", "collect", "web") for r in (1, 2, 3)]
    runs += [
        "dash-w4",
        "probes-trickle",
        "probes-hang",
        "probes-down",
        "unreachable-w4",
        "overflow",
        "pressure",
    ]
    for run in runs:
        lg = load(run, "load.json")
        if not lg:
            continue
        api = lg["api"]
        bad = sum(
            v
            for k, v in lg["statuses"].items()
            if not k.endswith(":200") and not k.startswith("_") and not k.startswith("dashboard")
        )
        db = load(run, "db.json") or {}
        files = db.get("files", {})
        size = (
            round(sum(v for k, v in files.items() if k.startswith("monitor.db")) / 2**20, 1) if files else "–"
        )
        flush = (db.get("data_quality") or {}).get("flush_failures", "–")
        print(
            f"| {run} | {api['throughput_rps']} | "
            f"{api['p50_ms']} / {api['p95_ms']} / {api['p99_ms']} | {bad} | "
            f"{lg['dashboard'].get('p95_ms') or '–'} | {rss_mib(load(run, 'proc_after.json'))} | {size} | "
            f"{completeness(run)} | {flush} |"
        )
    print()


def matrix_means() -> None:
    print("## 50 concurrent requests – mean of 3 repetitions\n")
    print("| Variant | Workers | rps | p50 ms | p95 ms | p99 ms | dashboard p95 ms | RSS after MiB |")
    print("|---|---|---|---|---|---|---|---|")
    for w in (1, 4):
        for v in ("off", "collect", "web"):
            runs = [f"matrix-{v}-w{w}-r{r}" for r in (1, 2, 3)]
            loads = [load(r, "load.json") for r in runs]
            loads = [x for x in loads if x]
            if not loads:
                continue
            api = [x["api"] for x in loads]
            dash = mean([x["dashboard"].get("p95_ms") for x in loads])
            rss = mean([rss_mib(load(r, "proc_after.json")) for r in runs])
            cells = [mean([a[k] for a in api]) for k in ("throughput_rps", "p50_ms", "p95_ms", "p99_ms")]
            print(f"| {v} | {w} | " + " | ".join(str(c) for c in cells) + f" | {dash or '–'} | {rss} |")
    print()


def details() -> None:
    for run in (
        "dash-w4",
        "probes-trickle",
        "probes-hang",
        "probes-down",
        "unreachable-w4",
        "overflow",
        "pressure",
    ):
        lg, db = load(run, "load.json"), load(run, "db.json")
        if not lg:
            continue
        print(f"### {run}\n")
        print("- statuses:", json.dumps({k: v for k, v in lg["statuses"].items() if not k.startswith("_")}))
        if lg.get("dashboard_by_page"):
            print(
                "- dashboard p95 by page (ms):",
                json.dumps({k: v["p95_ms"] for k, v in lg["dashboard_by_page"].items()}),
            )
        if db:
            print("- data quality:", json.dumps(db.get("data_quality")))
            print("- events:", json.dumps(db.get("events")))
            print("- files:", json.dumps(db.get("files")))
        if run == "pressure":
            for t in range(10, 70, 10):
                snap = load(run, f"db_t{t}.json")
                if snap:
                    total = sum(v for k, v in snap["files"].items() if k.startswith("monitor.db"))
                    print(
                        f"- t={t}s: telemetry files {round(total / 2**20, 1)} MiB, "
                        f"events {json.dumps(snap['events'])}"
                    )
            print(
                "- seed:",
                (ROOT / run / "seed.json").read_text().strip()
                if (ROOT / run / "seed.json").exists()
                else "–",
            )
        print()


overhead()
matrix_means()
matrix()
details()
