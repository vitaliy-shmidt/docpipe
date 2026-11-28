"""Release privacy check (Phase M7): end-to-end secret canaries.

Every input path carries a canary (documents, file names, headers, query
strings, prompts, AI answers, assistant questions, form passwords, unknown
paths), the configured upstream address is a canary host that the real
probes fail to reach, and an operator signs in with a canary password.
Afterwards no canary - and no session token, TOTP secret, password hash or
customer API key - may appear in:

- the raw bytes of every monitoring file (database, -wal, -shm, auth DB),
- any data API answer (all pages, several ranges) or HTML page,
- the log output.
"""

from __future__ import annotations

import dataclasses
import logging

from system.monitoring.settings import ProbeSettings
from system.monitoring.web.app import DATA_API, PAGES, SESSION_COOKIE
from tests.conftest import AI_KEY, ASSISTANT_KEY, LAB_KEY, VALID_KEY
from tests.monitoring_support import CANARY
from tests.monitoring_web_support import build_web_settings, full_login, user_entry, web_client
from tests.test_monitoring_security import _exercise_with_canaries

UPSTREAM_CANARY = "canary-upstream-7f3e9b"


def test_no_secret_reaches_files_api_html_or_logs(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    settings = build_web_settings(tmp_path)
    settings = dataclasses.replace(
        settings,
        ollama=dataclasses.replace(settings.ollama, base_url=f"http://{UPSTREAM_CANARY}.invalid:11434"),
        stirling=dataclasses.replace(settings.stirling, base_url=f"http://{UPSTREAM_CANARY}.invalid:8080"),
        monitoring=dataclasses.replace(
            settings.monitoring, probes=ProbeSettings(ollama=True, stirling=True, timeout_seconds=0.5)
        ),
    )
    entry = user_entry(password=CANARY + "-pw")
    with web_client(monkeypatch, tmp_path, settings=settings, users=[entry]) as client:
        monitor = client.app.state.monitoring
        _exercise_with_canaries(client, fake_stirling_holder["client"], fake_ollama_holder["client"])
        monitor.probe_now()  # real probes against the canary host (they fail)
        monitor.flush_now()
        monitor.maintenance_now()
        assert full_login(client, entry["totp_secret"], password=CANARY + "-pw").status_code == 303
        session_token = client.cookies.get(SESSION_COOKIE)
        bodies = []
        for path in PAGES:
            bodies.append(client.get(path).text)
        for path, name in DATA_API.items():
            for rng in ("1h", "24h", "7d"):
                params = {} if name in ("activity", "meta") else {"range": rng}
                response = client.get(path, params=params)
                assert response.status_code == 200, (path, response.text[:200])
                bodies.append(response.text)
        probes = client.get("/monitor/api/v1/ollama").json()["probes"]["recent"]
        assert probes and probes[0]["status"] in ("connection_error", "timeout")  # really probed
        monitor.flush_now()
        data_dir = settings.monitoring.data_dir
        raw = {p.name: p.read_bytes() for p in data_dir.iterdir() if p.is_file()}
    assert {"monitor.db", "monitor-auth.db"} <= set(raw)
    forbidden = [
        CANARY,
        CANARY.lower(),
        UPSTREAM_CANARY,
        session_token,
        entry["totp_secret"],
        entry["password_hash"],
        VALID_KEY,
        AI_KEY,
        LAB_KEY,
        ASSISTANT_KEY,
        "stirling-secret",
        "Bearer",
    ]
    for needle in forbidden:
        for name, data in raw.items():
            assert needle.encode() not in data, (needle, name)
            assert needle.encode("utf-16-le") not in data, (needle, name)
        for body in bodies:
            assert needle not in body, needle
    # What DocPipe itself logs (production runs at INFO). The test client's own httpx
    # logging and httpcore's DEBUG connection traces are not DocPipe output.
    own = "\n".join(
        r.getMessage() + (r.exc_text or "")
        for r in caplog.records
        if r.levelno >= logging.INFO and not r.name.startswith(("httpx", "httpcore"))
    )
    assert own  # DocPipe did log
    for needle in (CANARY, UPSTREAM_CANARY, session_token, entry["totp_secret"]):
        assert needle not in own, needle
