"""With monitoring disabled (the default) nothing changes (Phase M2)."""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from system import main as main_module
from system.monitoring.monitor import THREAD_NAME
from tests.conftest import AI_KEY, VALID_KEY, FakeOllamaClient, FakeStirlingClient, auth_headers, pdf_bytes
from tests.monitoring_support import monitored_client


def _requests(client) -> list:
    pdf = {"file": ("doc.pdf", pdf_bytes(), "application/pdf")}
    return [
        client.get("/api/v1/health"),
        client.get("/api/v1/capabilities", headers=auth_headers(VALID_KEY)),
        client.get("/api/v1/capabilities"),
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(VALID_KEY)),
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers("wrong")),
        client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "maintenance_extraction", "text": "Wartung", "context": {}},
        ),
        client.post(
            "/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json={"mode": "nope", "text": "x"}
        ),
        client.post("/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json={"broken": True}),
        client.get("/monitor"),
        client.get("/does-not-exist"),
    ]


def _comparable(response) -> tuple:
    headers = {
        k: v for k, v in response.headers.items() if k.lower() not in ("x-request-id", "content-length")
    }
    return response.status_code, response.content, headers


def test_disabled_monitoring_creates_nothing(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    responses = _requests(client)
    assert all(r.status_code != 500 for r in responses)
    assert client.app.state.monitoring is None
    assert not any(t.name == THREAD_NAME for t in threading.enumerate())
    assert list(tmp_path.iterdir()) == []  # no monitoring-data directory, no database
    # The routes see the unwrapped client objects.
    assert type(client.app.state.stirling_client) is FakeStirlingClient
    assert type(client.app.state.ollama_client) is FakeOllamaClient


def test_no_monitoring_routes_exist():
    paths = [getattr(r, "path", "") for r in main_module.app.routes]
    assert not any(p.startswith("/monitor") for p in paths)


def test_disabled_monitor_route_is_an_ordinary_404(client):
    unknown = client.get("/does-not-exist")
    monitor = client.get("/monitor")
    assert monitor.status_code == unknown.status_code == 404
    assert monitor.json() == unknown.json()


def test_responses_are_identical_with_monitoring_enabled(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with TestClient(main_module.app) as plain:
        baseline = [_comparable(r) for r in _requests(plain)]
        baseline_calls = (fake_stirling_holder["client"].extract_calls, fake_ollama_holder["client"].calls)
    with monitored_client(monkeypatch, tmp_path) as monitored:
        enabled = [_comparable(r) for r in _requests(monitored)]
        enabled_calls = (fake_stirling_holder["client"].extract_calls, fake_ollama_holder["client"].calls)
        # The response still carries an X-Request-ID, now the monitoring id.
        assert monitored.get("/api/v1/health").headers["x-request-id"]
    assert enabled == baseline
    # Same number of upstream calls: monitoring adds no Stirling/Ollama request.
    assert enabled_calls == baseline_calls


def test_monitoring_stops_cleanly_with_the_app(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        assert any(t.name == THREAD_NAME for t in threading.enumerate())
        client.get("/api/v1/health")
    assert main_module.app.state.monitoring is None
    assert not any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())
