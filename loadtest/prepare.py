"""Configs, users and measurements for the M7 load tests (synthetic only).

    python prepare.py config VARIANT [key=value ...]  -> /work/config.yaml
    python prepare.py users N                          -> /work/monitoring-users.yaml + users.json
    python prepare.py seed MIB                          -> grow /work/mon/monitoring.db to about MIB
    python prepare.py db                                -> JSON: rows, data quality, file sizes
    python prepare.py proc                              -> JSON: RSS / CPU of the uvicorn processes

VARIANT: off | collect | web   (web = collection + operator interface)
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
from pathlib import Path

import yaml

WORK = Path(os.environ.get("WORK", "/work"))
UPSTREAM = os.environ.get("UPSTREAM", "http://dpm7-fake:9000")


def config(variant: str, overrides: list[str]) -> None:
    monitoring = {
        "enabled": variant in ("collect", "web"),
        "data_dir": str(WORK / "mon"),
        "flush_interval_seconds": 5,
        "system_sample_interval_seconds": 15,
        "probe_interval_seconds": 30,
        "maintenance_interval_seconds": 60,
        "storage": {"max_bytes": 512 * 1024 * 1024, "min_free_disk_bytes": 0},
        "probes": {"ollama": True, "stirling": True, "timeout_seconds": 3},
        "web": {
            "enabled": variant == "web",
            "access_mode": "allowlist",
            "allowed_networks": ["127.0.0.1/32"],
            "require_https": False,
            "users_file": str(WORK / "monitoring-users.yaml"),
        },
    }
    for item in overrides:  # dotted.key=value, YAML-typed value
        key, value = item.split("=", 1)
        node = monitoring
        *path, last = key.split(".")
        for part in path:
            node = node.setdefault(part, {})
        node[last] = yaml.safe_load(value)
    services = {"documents": True, "ai": True, "prompt_lab": False, "assistant": False, "forms": False}
    cfg = {
        "server": {"max_file_size_mb": 25},
        "stirling": {"base_url": UPSTREAM, "api_key": "", "timeout_seconds": 30, "ocr": {"enabled": False}},
        "ollama": {"base_url": UPSTREAM, "keep_alive": "15m"},
        "prompts": {
            "base_dir": "system/prompts",
            "runtime_dir": str(WORK / "runtime-prompts"),
            "max_content_length": 32000,
        },
        "models": {
            name: {"provider": "ollama", "model": "synthetic-model", "timeout_seconds": 30, "temperature": 0}
            for name in ("light", "standard", "heavy")
        },
        "monitoring": monitoring,
        "clients": {
            "load-documents": {"enabled": True, "api_key": "load-documents-key", "services": services},
            "load-ai": {"enabled": True, "api_key": "load-ai-key", "services": services},
        },
    }
    (WORK / "runtime-prompts").mkdir(parents=True, exist_ok=True)
    (WORK / "mon").mkdir(parents=True, exist_ok=True)
    (WORK / "config.yaml").write_text(yaml.safe_dump(cfg, sort_keys=False))


def users(n: int) -> None:
    from system.monitoring.web import passwords, totp

    entries, plain = [], []
    for i in range(n):
        password = secrets.token_urlsafe(18)
        secret = totp.new_secret()
        name = f"loadop{i}"
        entries.append(
            {
                "username": name,
                "password_hash": passwords.hash_password(password),
                "totp_secret": secret,
                "enabled": True,
            }
        )
        plain.append({"username": name, "password": password, "totp_secret": secret})
    path = WORK / "monitoring-users.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "users": entries}))
    os.chmod(path, 0o600)
    (WORK / "users.json").write_text(json.dumps(plain))
    os.chmod(WORK / "users.json", 0o600)


def seed(mib: float) -> None:
    """Grow the telemetry DB with synthetic requests (through the real store)."""
    import random

    from system.monitoring.context import StepRecord
    from system.monitoring.recorder import Batch, FinishedRequest
    from system.monitoring.settings import MonitoringSettings
    from system.monitoring.store import MonitoringStore

    store = MonitoringStore(MonitoringSettings(enabled=True, data_dir=WORK / "mon"))
    store.open()
    import time

    now = int(time.time() * 1000)
    from system.monitoring.buildinfo import read_build_info

    store.register_instance(
        instance_id="seed", pid=1, now_ms=now, app_version="seed", build=read_build_info({})
    )
    rng = random.Random(1)
    i = 0
    main_file = WORK / "mon" / "monitor.db"
    while main_file.stat().st_size < mib * 1024 * 1024:  # main file: the WAL is checkpointed below
        batch = []
        for _ in range(2000):
            i += 1
            ts = now - rng.randrange(6 * 86_400_000)
            batch.append(
                FinishedRequest(
                    request_id=f"seed-{i}",
                    ts_ms=ts,
                    duration_ms=rng.uniform(5, 900),
                    client_id="load-documents",
                    route="/api/v1/documents/extract-text",
                    method="POST",
                    service="documents",
                    status_code=200,
                    error_code=None,
                    bytes_in=4000,
                    bytes_out=900,
                    documents=1,
                    pages=1,
                    ai_provider=None,
                    ai_model=None,
                    ai_calls=0,
                    tokens_in=None,
                    tokens_out=None,
                    steps_truncated=0,
                    steps=[
                        StepRecord(
                            step="stirling.extract_text", outcome="ok", duration_ms=rng.uniform(5, 400)
                        )
                    ],
                )
            )
        store.write_batch("seed", Batch(batch, [], {}), now, [])
        store.checkpoint()
    store.checkpoint()
    print(json.dumps({"seeded_requests": i, "bytes": store.file_bytes()}))
    store.close()


def db() -> None:
    mon = WORK / "mon"
    path = mon / "monitor.db"
    out = {"files": {p.name: p.stat().st_size for p in sorted(mon.glob("*")) if p.is_file()}}
    if path.exists():
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
        out["request_events"] = q("SELECT COUNT(*) FROM request_events")
        out["request_steps"] = q("SELECT COUNT(*) FROM request_steps")
        out["by_route"] = dict(conn.execute("SELECT route, COUNT(*) FROM request_events GROUP BY route"))
        cols = [r[1] for r in conn.execute("PRAGMA table_info(data_quality)")]
        sums = [c for c in cols if c not in ("bucket_ms", "instance_id")]
        row = conn.execute(f"SELECT {', '.join(f'SUM({c})' for c in sums)} FROM data_quality").fetchone()
        out["data_quality"] = dict(zip(sums, row))
        out["events"] = dict(conn.execute("SELECT code, SUM(repeat_count) FROM events GROUP BY code"))
        out["user_version"] = q("PRAGMA user_version")
        conn.close()
    print(json.dumps(out))


def proc() -> None:
    procs = []
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode()
            if "uvicorn" not in cmd and "multiprocessing" not in cmd:
                continue
            status = Path(f"/proc/{pid}/status").read_text()
            rss = next(int(line.split()[1]) for line in status.splitlines() if line.startswith("VmRSS"))
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            cpu_ticks = int(stat[11]) + int(stat[12])
            procs.append(
                {
                    "pid": int(pid),
                    "rss_kib": rss,
                    "cpu_ticks": cpu_ticks,
                    "threads": next(
                        int(line.split()[1]) for line in status.splitlines() if line.startswith("Threads")
                    ),
                }
            )
        except (OSError, StopIteration, ValueError):
            continue
    cg = {}
    for name in ("cpu.stat", "memory.current", "memory.peak"):
        try:
            cg[name] = Path(f"/sys/fs/cgroup/{name}").read_text().strip()
        except OSError:
            pass
    print(json.dumps({"processes": procs, "clk_tck": os.sysconf("SC_CLK_TCK"), "cgroup": cg}))


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "config":
        config(sys.argv[2], sys.argv[3:])
    elif cmd == "users":
        users(int(sys.argv[2]))
    elif cmd == "seed":
        seed(float(sys.argv[2]))
    elif cmd == "db":
        db()
    elif cmd == "proc":
        proc()
