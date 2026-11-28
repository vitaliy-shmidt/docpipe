"""Ollama/Stirling status probes: GET-only metadata, defensive parsing (Phase M2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from system.monitoring import monitor as monitor_module
from system.monitoring.monitor import Monitor
from system.monitoring.probes import ServiceProber
from system.monitoring.settings import ProbeSettings
from tests.monitoring_support import build_settings, db_rows, monitored_client

ALLOWED = {"/api/version", "/api/tags", "/api/ps", "/api/v1/info/status"}

TAGS = {
    "models": [
        {
            "name": "qwen2.5:1.5b-instruct",
            "size": 986_000_000,
            "details": {"family": "qwen2", "parameter_size": "1.5B", "quantization_level": "Q4_K_M"},
        },
        {"name": "evil\nname", "size": 1},
        {"name": "llama3.2:3b", "size": 2_000_000_000, "details": {"quantization_level": "Q8_0"}},
    ]
}
PS = {
    "models": [
        {
            "name": "qwen2.5:1.5b-instruct",
            "size": 1_200_000_000,
            "size_vram": 0,
            "expires_at": "2026-11-26T12:30:00.123456789+01:00",
            "context_length": 8192,
        }
    ]
}


class Upstream:
    """httpx MockTransport recording every request."""

    def __init__(self, routes: dict | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.routes = routes or {
            "/api/version": (200, {"version": "0.40.18"}),
            "/api/tags": (200, TAGS),
            "/api/ps": (200, PS),
            "/api/v1/info/status": (200, {"status": "UP", "version": "3.2.2"}),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path, (404, {}))
        if isinstance(route, Exception):
            raise route
        status, body = route
        content = body if isinstance(body, bytes) else json.dumps(body).encode()
        return httpx.Response(status, content=content)


def prober(upstream: Upstream, **kwargs) -> ServiceProber:
    options = dict(
        ollama_url="http://ollama.test",
        stirling_url="http://stirling.test",
        probe_ollama=True,
        probe_stirling=True,
        timeout_seconds=1,
    )
    options.update(kwargs)
    return ServiceProber(transport=httpx.MockTransport(upstream), **options)


def test_probes_read_versions_models_and_loaded_state():
    upstream = Upstream()
    p = prober(upstream)
    stirling = p.probe_stirling()
    ollama, models = p.probe_ollama()
    assert (stirling.status, stirling.version, stirling.http_status) == ("ok", "3.2.2", 200)
    assert (ollama.status, ollama.version) == ("ok", "0.40.18")
    by_name = {m.name: m for m in models}
    assert set(by_name) == {"qwen2.5:1.5b-instruct", "llama3.2:3b"}  # the malformed name is dropped
    qwen = by_name["qwen2.5:1.5b-instruct"]
    assert (qwen.loaded, qwen.size_vram_bytes, qwen.context_length, qwen.quantization) == (
        True,
        0,
        8192,
        "Q4_K_M",
    )
    # 12:30:00.123456 at +01:00 = 11:30:00.123 UTC (nanoseconds truncated to micro-, stored as ms)
    expected = datetime(2026, 11, 26, 11, 30, 0, 123456, tzinfo=timezone.utc)
    assert qwen.expires_ms == int(expected.timestamp() * 1000)
    assert by_name["llama3.2:3b"].loaded is False and by_name["llama3.2:3b"].size_vram_bytes is None


def test_probes_only_issue_get_requests_to_metadata_endpoints():
    upstream = Upstream()
    p = prober(upstream)
    for _ in range(3):
        p.probe_stirling()
        p.probe_ollama()
    assert upstream.requests and all(r.method == "GET" for r in upstream.requests)
    assert {r.url.path for r in upstream.requests} <= ALLOWED
    # No Stirling API key and no credentials are sent with a probe.
    assert all("x-api-key" not in r.headers and "authorization" not in r.headers for r in upstream.requests)


@pytest.mark.parametrize(
    "failure, expected",
    [
        (httpx.ConnectError("refused"), "connection_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        ((500, {"error": "boom"}), "http_error"),
        ((200, b"not json"), "invalid_response"),
        ((200, b"[1, 2]"), "invalid_response"),
        ((200, b"x" * (1024 * 1024 + 10)), "invalid_response"),
        ((302, {}), "http_error"),
    ],
)
def test_failures_are_classified(failure, expected):
    upstream = Upstream({"/api/version": failure, "/api/v1/info/status": failure})
    p = prober(upstream)
    assert p.probe_stirling().status == expected
    result, models = p.probe_ollama()
    assert result.status == expected and models == []


def test_stirling_status_other_than_up_is_not_ok():
    p = prober(Upstream({"/api/v1/info/status": (200, {"status": "DOWN", "version": "3.2.2"})}))
    assert p.probe_stirling().status == "invalid_response"


def test_older_ollama_without_new_fields_is_tolerated():
    upstream = Upstream(
        {
            "/api/version": (200, {"version": "0.5.1"}),
            "/api/tags": (200, {"models": [{"name": "m:latest"}]}),
            "/api/ps": (200, {"models": [{"name": "m:latest", "expires_at": "0001-01-01T00:00:00Z"}]}),
        }
    )
    result, [model] = prober(upstream).probe_ollama()
    assert result.status == "ok"
    assert (model.loaded, model.size_vram_bytes, model.context_length, model.expires_ms) == (
        True,
        None,
        None,
        None,
    )


def test_disabled_or_unconfigured_probes_send_nothing():
    upstream = Upstream()
    assert prober(upstream, probe_ollama=False).probe_ollama()[0].status == "not_configured"
    assert prober(upstream, stirling_url="").probe_stirling().status == "not_configured"
    assert upstream.requests == []


def test_monitor_records_probes_and_transition_events(tmp_path, monkeypatch):
    upstream = Upstream()
    settings = build_settings(tmp_path, probes=ProbeSettings(ollama=True, stirling=True))
    mon = Monitor(settings, env={}, prober=prober(upstream))
    mon.store.open()
    mon.probe_now()
    upstream.routes["/api/v1/info/status"] = httpx.ConnectError("down")
    mon.probe_now()
    mon.probe_now()  # still down: no second event
    upstream.routes["/api/v1/info/status"] = (200, {"status": "UP", "version": "3.2.2"})
    mon.probe_now()
    mon.flush_now()
    probes = db_rows(
        mon.store.path, "SELECT service, status FROM service_probes WHERE service = 'stirling' ORDER BY id"
    )
    events = db_rows(
        mon.store.path, "SELECT code, service, detail FROM events WHERE service = 'stirling' ORDER BY id"
    )
    models = db_rows(mon.store.path, "SELECT DISTINCT name, loaded FROM ollama_models ORDER BY name")
    mon.store.close()
    assert [s for _, s in probes] == ["ok", "connection_error", "connection_error", "ok"]
    assert events == [
        ("service_unreachable", "stirling", "connection_error"),
        ("service_recovered", "stirling", None),
    ]
    assert models == [("llama3.2:3b", 0), ("qwen2.5:1.5b-instruct", 1)]


def test_enabled_app_never_generates_loads_models_or_processes_pdfs(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    upstream = Upstream()
    real = monitor_module.ServiceProber
    monkeypatch.setattr(
        monitor_module, "ServiceProber", lambda **kw: real(transport=httpx.MockTransport(upstream), **kw)
    )
    with monitored_client(monkeypatch, tmp_path, probes=ProbeSettings(ollama=True, stirling=True)) as client:
        for _ in range(3):
            client.monitor.probe_now()
            client.monitor.sample_now()
            client.monitor.maintenance_now()
            client.monitor.flush_now()
        assert fake_ollama_holder["client"].calls == 0 and fake_ollama_holder["client"].warmup_calls == 0
        assert (
            fake_stirling_holder["client"].extract_calls == 0
            and fake_stirling_holder["client"].ocr_calls == 0
        )
        assert fake_stirling_holder["client"].convert_calls == []
    assert {(r.method, r.url.path) for r in upstream.requests} <= {("GET", p) for p in ALLOWED}
