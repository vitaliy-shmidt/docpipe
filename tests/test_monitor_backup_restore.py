"""Backup and restore of both monitoring databases (Phase M7).

Exercises exactly the procedure documented in
docs/operations/monitoring-operations.md: SQLite's online backup API, run
while DocPipe keeps writing (WAL mode), then a restore into an empty data
directory. Synthetic data only.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import threading

import pytest

from system.monitoring.store import MonitoringStore
from system.monitoring.web.authstore import AUTH_DB_FILENAME, AuthStore
from tests.conftest import VALID_KEY, auth_headers
from tests.monitoring_web_support import full_login, user_entry, web_client


def backup(source, target) -> None:
    """The documented procedure (see the operations guide): consistent online
    copy, private file permissions, integrity check of the copy."""
    old = os.umask(0o077)
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        assert dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        dst.close()
        src.close()
    finally:
        os.umask(old)


def test_online_backup_during_writes_and_restore(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    entry = user_entry()
    backups = tmp_path / "backup"
    backups.mkdir(mode=0o700)
    with web_client(monkeypatch, tmp_path, users=[entry]) as client:
        monitor = client.app.state.monitoring
        for _ in range(50):
            client.get("/api/v1/health", headers=auth_headers(VALID_KEY))
        monitor.flush_now()
        assert full_login(client, entry["totp_secret"]).status_code == 303
        data_dir = monitor.settings.data_dir
        stop = threading.Event()

        def writer():  # DocPipe keeps working during the backup
            while not stop.is_set():
                client.get("/api/v1/health")
                monitor.flush_now()

        thread = threading.Thread(target=writer)
        thread.start()
        try:
            backup(data_dir / "monitor.db", backups / "monitor.db")
            backup(data_dir / AUTH_DB_FILENAME, backups / AUTH_DB_FILENAME)
        finally:
            stop.set()
            thread.join(10)
        live_sessions = client.web.store._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    assert live_sessions >= 1

    if os.name == "posix":
        for name in ("monitor.db", AUTH_DB_FILENAME):
            assert stat.S_IMODE((backups / name).stat().st_mode) == 0o600

    restored = tmp_path / "restored"
    restored.mkdir(mode=0o700)
    for name in ("monitor.db", AUTH_DB_FILENAME):  # only the main files: -wal/-shm are not part of a backup
        (restored / name).write_bytes((backups / name).read_bytes())
    store = MonitoringStore(monitor.settings.__class__(enabled=True, data_dir=restored))
    store.open()
    try:
        assert store.schema_version() == 3
        db = store._db()
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("SELECT COUNT(*) FROM request_events").fetchone()[0] >= 50
    finally:
        store.close()
    auth = AuthStore(restored / AUTH_DB_FILENAME, 64 * 1024 * 1024)
    auth.open()
    try:
        assert auth._db().execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 1  # sessions survive
        assert auth._db().execute("SELECT COUNT(*) FROM audit").fetchone()[0] >= 1
    finally:
        auth.close()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_restored_files_get_private_permissions(tmp_path):
    """A restore copied with a permissive umask is tightened when DocPipe opens it."""
    restored = tmp_path / "restored"
    restored.mkdir()
    sqlite3.connect(restored / "monitor.db").close()
    os.chmod(restored / "monitor.db", 0o644)
    from system.monitoring.settings import MonitoringSettings

    store = MonitoringStore(MonitoringSettings(enabled=True, data_dir=restored))
    store.open()
    store.close()
    assert stat.S_IMODE((restored / "monitor.db").stat().st_mode) == 0o600
