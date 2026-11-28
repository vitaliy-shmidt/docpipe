"""Helpers for the monitoring tests (synthetic data and fakes only)."""

from __future__ import annotations

import dataclasses
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from system import main as main_module
from system.monitoring.settings import MonitoringSettings, ProbeSettings, StorageSettings
from tests.conftest import _build_test_settings

CANARY = "CANARY-7f3e9b-SECRET"


def monitoring_settings(data_dir: Path, **overrides) -> MonitoringSettings:
    values = dict(
        enabled=True,
        data_dir=data_dir,
        # Large intervals: tests drive flush/sample/probe explicitly.
        flush_interval_seconds=30,
        system_sample_interval_seconds=3600,
        probe_interval_seconds=3600,
        maintenance_interval_seconds=3600,
        probes=ProbeSettings(ollama=False, stirling=False),
        storage=StorageSettings(min_free_disk_bytes=0),
    )
    values.update(overrides)
    return MonitoringSettings(**values)


def build_settings(tmp_path: Path, **monitoring_overrides):
    base = _build_test_settings(tmp_path / "runtime-prompts")
    return dataclasses.replace(base, monitoring=monitoring_settings(tmp_path / "mon", **monitoring_overrides))


@contextmanager
def monitored_client(monkeypatch, tmp_path: Path, settings=None, **monitoring_overrides):
    """TestClient with monitoring enabled. Requires the conftest fake
    Stirling/Ollama fixtures to be active (they patch the client factories)."""
    settings = settings or build_settings(tmp_path, **monitoring_overrides)
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    with TestClient(main_module.app, raise_server_exceptions=False) as client:
        client.monitor = main_module.app.state.monitoring
        yield client


def db_rows(path: Path, sql: str, params=()) -> list[tuple]:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def dump_text(path: Path) -> str:
    """Every value of every table (including -wal content via a normal read)."""
    conn = sqlite3.connect(str(path))
    try:
        parts = []
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for row in conn.execute(f"SELECT * FROM {table}"):
                parts.append(" ".join(str(v) for v in row))
        return "\n".join(parts)
    finally:
        conn.close()
