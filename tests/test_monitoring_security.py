"""No secrets, contents or free-form strings reach the monitoring store (Phase M2)."""

from __future__ import annotations

import re

from system.monitoring import registry
from tests.conftest import AI_KEY, ASSISTANT_KEY, LAB_KEY, VALID_KEY, auth_headers, pdf_bytes
from tests.monitoring_support import CANARY, db_rows, dump_text, monitored_client

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _exercise_with_canaries(client, fake_stirling, fake_ollama) -> None:
    canary_headers = {
        "User-Agent": CANARY,
        "X-Forwarded-For": CANARY,
        "X-Api-Key": CANARY,
        "Cookie": f"session={CANARY}",
        "Referer": f"https://x/{CANARY}",
    }
    fake_stirling.text = f"Vertrag {CANARY} Mueller GmbH " * 3
    fake_ollama.result = {**fake_ollama.result, "notes": CANARY}
    pdf = {"file": (f"{CANARY}-contract.pdf", pdf_bytes(), "application/pdf")}
    client.post(
        f"/api/v1/documents/extract-text?q={CANARY}",
        files=pdf,
        headers={**auth_headers(VALID_KEY), **canary_headers},
    )
    client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(CANARY))
    client.post("/api/v1/documents/extract-text", files=pdf, headers={"Authorization": CANARY})
    client.post(
        "/api/v1/documents/convert-to-pdf",
        headers=auth_headers(VALID_KEY),
        files={"file": (f"{CANARY}.txt", CANARY.encode(), "text/plain")},
        data={"source_format": "txt"},
    )
    client.post(
        "/api/v1/documents/convert-to-pdf",
        headers=auth_headers(VALID_KEY),
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"source_format": CANARY},
    )
    client.post(
        "/api/v1/documents/analyze",
        headers={**auth_headers(AI_KEY), **canary_headers},
        json={"mode": "maintenance_extraction", "text": CANARY, "context": {"customer": CANARY}},
    )
    # DocPipeError messages that embed request input (unknown_mode) must not be stored.
    client.post("/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json={"mode": CANARY, "text": "x"})
    client.post(
        "/api/v1/assistant/query",
        headers=auth_headers(ASSISTANT_KEY),
        json={"question": CANARY, "context": {"x": CANARY}, "domains": ["contracts"]},
    )
    client.get(f"/api/v1/lab/prompts/{CANARY}", headers=auth_headers(LAB_KEY))
    client.post(
        "/api/v1/lab/analyze",
        headers=auth_headers(LAB_KEY),
        json={
            "mode": "maintenance_extraction",
            "prompt": f"{CANARY} {{text}}",
            "text": CANARY,
            "context": {},
        },
    )
    client.post(
        "/api/v1/documents/forms/inspect",
        headers=auth_headers(VALID_KEY),
        files=pdf,
        data={"password": CANARY},
    )
    client.get(f"/{CANARY}/%0d%0aInjected: {CANARY}")


def test_canaries_never_reach_the_database(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with monitored_client(monkeypatch, tmp_path) as client:
        _exercise_with_canaries(client, fake_stirling_holder["client"], fake_ollama_holder["client"])
        client.monitor.flush_now()
        path = client.monitor.store.path
        count = db_rows(path, "SELECT COUNT(*) FROM request_events")[0][0]
        text = dump_text(path)
    assert count >= 12
    assert CANARY not in text
    for secret in ("Bearer", VALID_KEY, AI_KEY, "stirling-secret", "Mueller", "contract.pdf"):
        assert secret not in text
    # Nothing in the files either (WAL included), checked on the raw bytes.
    for file in path.parent.iterdir():
        assert CANARY.encode() not in file.read_bytes()


def test_log_injection_is_impossible_because_only_registry_values_are_stored(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        for path in (
            "/x%0d%0aFAKE%20LINE",
            "/%1b[31mred",
            "/api/v1/lab/prompts/a%0Ab",
            "/" + "A" * 5000,
            "/api/v1/health/%00",
        ):
            client.get(path, headers={"User-Agent": "a\tb", "X-Request-ID": "evil\r\nid"})
        _exercise_with_canaries(client, fake_stirling_holder["client"], fake_ollama_holder["client"])
        client.monitor.flush_now()
        db = client.monitor.store.path
        tables = [r[0] for r in db_rows(db, "SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            for row in db_rows(db, f"SELECT * FROM {table}"):
                for value in row:
                    if isinstance(value, str):
                        assert not _CONTROL.search(value), (table, value)
                        assert len(value) <= 200, (table, value)
        request_ids = [r[0] for r in db_rows(db, "SELECT request_id FROM request_events")]
    # The client-sent X-Request-ID is never adopted.
    assert all(re.fullmatch(r"[0-9a-f-]{36}", rid) for rid in request_ids)


def test_every_stored_dimension_comes_from_a_registry_or_the_config(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        _exercise_with_canaries(client, fake_stirling_holder["client"], fake_ollama_holder["client"])
        client.monitor.flush_now()
        db = client.monitor.store.path
        known_clients = set(client.app.state.settings.clients)
        known_models = {p.model for p in client.app.state.settings.models.values()}
        templates = {getattr(r, "path", None) for r in client.app.routes} | set(registry.SERVICE_BY_ROUTE)
        rows = db_rows(
            db, "SELECT client_id, route, method, service, error_code, ai_model FROM request_events"
        )
        steps = db_rows(db, "SELECT step, outcome, ai_model FROM request_steps")
        events = db_rows(
            db, "SELECT severity, code, service, client_id, route, error_code, detail FROM events"
        )
    for client_id, route, method, service, error_code, model in rows:
        assert client_id is None or client_id in known_clients
        assert route in templates or route == registry.UNMATCHED_ROUTE
        assert method in registry.METHODS and service in registry.SERVICES
        assert error_code is None or error_code in registry.ERROR_CODES
        assert model is None or model in known_models
    for step, outcome, model in steps:
        assert step in registry.STEPS
        assert outcome in registry.ERROR_CODES | {"ok", "error"}
        assert model is None or model in known_models
    for severity, code, service, client_id, route, error_code, detail in events:
        assert severity in registry.SEVERITIES and code in registry.EVENT_CODES
        assert service in registry.EVENT_SERVICES


def test_registry_helpers_reject_free_form_values():
    assert registry.error_code("ai_timeout") == "ai_timeout"
    assert registry.error_code("anything\nelse") == registry.UNKNOWN_ERROR
    assert registry.outcome("weird") == registry.OUTCOME_ERROR
    assert registry.safe_id("qwen2.5:1.5b-instruct") == "qwen2.5:1.5b-instruct"
    assert registry.safe_id("bad name") is None and registry.safe_id("x\n") is None
    assert registry.safe_version("0.40.18") == "0.40.18" and registry.safe_version("1 2") is None
    assert registry.safe_route("/api/v1/lab/prompts/{mode}") and not registry.safe_route("/a b")
