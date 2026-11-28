"""Request recording end to end with monitoring enabled (Phase M2).

Upstreams are the conftest fakes; nothing leaves the process.
"""

from __future__ import annotations

import dataclasses
import json
from concurrent.futures import ThreadPoolExecutor

from system.config import ClientConfig, ClientServices
from system.errors import DocPipeError
from tests.conftest import AI_KEY, ASSISTANT_KEY, VALID_KEY, auth_headers, pdf_bytes
from tests.monitoring_support import build_settings, db_rows, monitored_client
from tests.test_forms_fill import fields_of, manifest, native
from tests.test_forms_inspect import fixture

FORMS_KEY = "mon-forms-key"


def _events(client) -> list[tuple]:
    client.monitor.flush_now()
    return db_rows(
        client.monitor.store.path,
        "SELECT request_id, client_id, route, service, status_code, error_code, documents, pages, "
        "ai_model, ai_calls, tokens_in, tokens_out, bytes_in, bytes_out FROM request_events ORDER BY id",
    )


def _steps(client, request_id: str) -> list[tuple]:
    return db_rows(
        client.monitor.store.path,
        "SELECT step, outcome, ai_model, tokens_in, tokens_out, repair_used FROM request_steps "
        "WHERE request_id = ? ORDER BY seq",
        (request_id,),
    )


def _pdf():
    return {"file": ("contract-mueller.pdf", pdf_bytes(), "application/pdf")}


def test_request_is_recorded_with_template_service_and_authenticated_client(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        response = client.post(
            "/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY)
        )
        [row] = _events(client)
    request_id, client_id, route, service, status, error, documents, *_, bytes_in, bytes_out = row
    assert request_id == response.headers["x-request-id"]  # correlates with log line and response
    assert (client_id, route, service, status, error, documents) == (
        "enabled-client",
        "/api/v1/documents/extract-text",
        "documents",
        200,
        None,
        1,
    )
    assert bytes_out == len(response.content) and bytes_in > len(pdf_bytes())
    assert _steps(client, request_id) == [("stirling.extract_text", "ok", None, None, None, 0)]


def test_client_attribution_comes_only_from_the_api_key(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    spoof = {"X-Client-Id": "ai-client", "X-Forwarded-User": "ai-client"}
    with monitored_client(monkeypatch, tmp_path) as client:
        client.post(
            "/api/v1/documents/extract-text",
            files=_pdf(),
            data={"client_id": "ai-client"},
            headers={**auth_headers(VALID_KEY), **spoof},
        )
        client.post(
            "/api/v1/documents/extract-text", files=_pdf(), headers={**auth_headers("unknown"), **spoof}
        )
        client.post(
            "/api/v1/documents/analyze",
            headers={**auth_headers(VALID_KEY), **spoof},
            json={"mode": "maintenance_extraction", "text": "x", "context": {"client_id": "ai-client"}},
        )
        rows = _events(client)
    assert [(r[1], r[4], r[5]) for r in rows] == [
        ("enabled-client", 200, None),
        (None, 401, "unauthorized"),
        ("enabled-client", 403, "ai_disabled"),
    ]


def test_path_parameters_and_unknown_paths_are_never_stored(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        client.get("/api/v1/lab/prompts/secret-mode-name", headers=auth_headers(VALID_KEY))
        client.get("/wp-admin/../../etc/passwd")
        client.get("/api/v1/health?token=abc")
        rows = _events(client)
    assert [r[2] for r in rows] == ["/api/v1/lab/prompts/{mode}", "<unmatched>", "/api/v1/health"]
    assert [r[3] for r in rows] == ["prompt_lab", "none", "system"]


def test_upstream_failures_become_codes_steps_and_events(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        fake_stirling_holder["client"].mode = "timeout"
        response = client.post(
            "/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY)
        )
        fake_ollama_holder["client"].mode = "unavailable"
        client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "maintenance_extraction", "text": "x", "context": {}},
        )
        rows = _events(client)
        events = db_rows(
            client.monitor.store.path,
            "SELECT code, severity, service, error_code, request_id FROM events "
            "WHERE code = 'upstream_error' ORDER BY id",
        )
        steps = _steps(client, rows[0][0])
    assert response.status_code == 504
    assert [(r[4], r[5]) for r in rows] == [(504, "timeout"), (502, "ai_unavailable")]
    assert steps == [("stirling.extract_text", "timeout", None, None, None, 0)]
    assert [(e[0], e[1], e[2], e[3]) for e in events] == [
        ("upstream_error", "WARNING", "stirling", "timeout"),
        ("upstream_error", "WARNING", "ollama", "ai_unavailable"),
    ]
    assert events[0][4] == rows[0][0]


def test_ocr_fallback_is_split_into_steps(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    with monitored_client(monkeypatch, tmp_path) as client:
        fake_stirling_holder["client"].text = ""
        client.post("/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY))
        [row] = _events(client)
        assert [s[0] for s in _steps(client, row[0])] == [
            "stirling.extract_text",
            "stirling.ocr",
            "stirling.extract_text",
        ]


def test_ollama_metadata_is_reused_for_tokens_and_model(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        fake = fake_ollama_holder["client"]
        fake.fake_prompt_eval_count, fake.fake_eval_count = 120, 30
        client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "maintenance_extraction", "text": "Wartung", "context": {}},
        )
        fake.fake_prompt_eval_count = None  # an Ollama that does not report counts
        client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "maintenance_extraction", "text": "Wartung", "context": {}},
        )
        client.post("/api/v1/assistant/warmup", headers=auth_headers(ASSISTANT_KEY), json={})
        rows = _events(client)
        known, unknown = rows[0], rows[1]
        assert (known[8], known[9], known[10], known[11]) == ("light-test-model", 1, 120, 30)
        assert (unknown[9], unknown[10], unknown[11]) == (1, None, None)  # unknown stays unknown, not 0
        assert _steps(client, known[0]) == [("ai.generate", "ok", "light-test-model", 120, 30, 0)]
        warmup_steps = _steps(client, rows[2][0])
    assert [s[0] for s in warmup_steps] == ["ai.warmup"]
    assert fake.calls == 2 and fake.warmup_calls == 1  # exactly the requests' own calls


def test_unhandled_exception_is_recorded_and_still_answered_as_before(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:

        def boom(*args, **kwargs):
            raise RuntimeError("internal detail that must not be stored")

        monkeypatch.setattr(fake_stirling_holder["client"], "extract_text", boom)
        response = client.post(
            "/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY)
        )
        [row] = _events(client)
        events = db_rows(
            client.monitor.store.path,
            "SELECT code, severity, route FROM events WHERE code = 'unhandled_exception'",
        )
        step = _steps(client, row[0])
    assert response.status_code == 500
    assert response.json() == {
        "ok": False,
        "code": "internal_error",
        "message": "An unexpected error occurred.",
    }
    assert (row[4], row[5]) == (500, "internal_error")
    assert events == [("unhandled_exception", "ERROR", "/api/v1/documents/extract-text")]
    assert step == [("stirling.extract_text", "error", None, None, None, 0)]


def test_fifty_parallel_requests_are_counted_exactly(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:

        def call(i: int):
            if i % 2:
                return client.post(
                    "/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY)
                )
            return client.get("/api/v1/capabilities", headers=auth_headers(AI_KEY))

        with ThreadPoolExecutor(max_workers=50) as pool:
            responses = list(pool.map(call, range(100)))
        assert all(r.status_code == 200 for r in responses)
        rows = _events(client)
        health = client.monitor.health()
        stats = client.monitor.store.request_stats(0, 2**62, client.monitor._clock() + 1)
    assert len(rows) == 100 and len({r[0] for r in rows}) == 100
    assert {r[0] for r in rows} == {r.headers["x-request-id"] for r in responses}
    assert sum(1 for r in rows if r[1] == "enabled-client") == 50
    assert sum(1 for r in rows if r[1] == "ai-client") == 50
    assert health["active_requests"] == 0 and health["totals"]["requests_dropped_buffer"] == 0
    assert stats["count"] == 100 and stats["completeness"]["complete"] is True


def test_forms_pages_come_from_the_existing_result(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    settings = build_settings(tmp_path)
    forms = ClientConfig(
        client_id="mon-forms",
        enabled=True,
        api_key=FORMS_KEY,
        services=ClientServices(documents=True, forms=True),
    )
    settings = dataclasses.replace(settings, clients={**settings.clients, "mon-forms": forms})
    with monitored_client(monkeypatch, tmp_path, settings=settings) as client:
        files = {"file": ("form.pdf", fixture("basic-form.pdf"), "application/pdf")}
        inspect = client.post("/api/v1/documents/forms/inspect", files=files, headers=auth_headers(FORMS_KEY))
        f = fields_of("basic-form.pdf")
        m = manifest("basic-form.pdf", [native(f["vorname"], "Wert")])
        fill = client.post(
            "/api/v1/documents/forms/fill",
            headers=auth_headers(FORMS_KEY),
            files=[("file", ("t.pdf", fixture("basic-form.pdf"), "application/pdf"))],
            data={"manifest": json.dumps(m)},
        )
        rows = _events(client)
        inspect_steps = _steps(client, rows[0][0])
        fill_steps = _steps(client, rows[1][0])
    assert inspect.status_code == 200 and fill.status_code == 200
    assert (rows[0][1], rows[0][6], rows[0][7]) == ("mon-forms", 1, inspect.json()["data"]["page_count"])
    assert rows[1][7] == 1
    assert [s[0] for s in inspect_steps] == ["forms.inspect"]
    assert [s[0] for s in fill_steps] == ["forms.fill"]


def test_extract_text_page_count_stays_unknown(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        client.post("/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY))
        [row] = _events(client)
    assert row[7] is None  # no PDF is parsed just to count pages


def test_failing_monitoring_hooks_never_affect_the_api(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:
        monitor = client.monitor

        def broken(*args, **kwargs):
            raise RuntimeError("monitoring bug")

        monkeypatch.setattr(monitor, "end", broken)
        assert (
            client.post(
                "/api/v1/documents/extract-text", files=_pdf(), headers=auth_headers(VALID_KEY)
            ).status_code
            == 200
        )
        monkeypatch.setattr(monitor, "begin", broken)
        assert client.get("/api/v1/health").status_code == 200
        assert monitor.health()["totals"]["hook_failures"] == 2


def test_docpipe_error_raised_by_a_step_keeps_its_code(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    with monitored_client(monkeypatch, tmp_path) as client:

        def denied(*args, **kwargs):
            raise DocPipeError("upstream_auth_failed", "Document processing service rejected the request.")

        monkeypatch.setattr(fake_stirling_holder["client"], "convert_to_pdf", denied)
        response = client.post(
            "/api/v1/documents/convert-to-pdf",
            headers=auth_headers(VALID_KEY),
            files={"file": ("a.txt", b"hello", "text/plain")},
            data={"source_format": "txt"},
        )
        [row] = _events(client)
        assert response.status_code == 502 and row[5] == "upstream_auth_failed"
        assert _steps(client, row[0]) == [("stirling.convert", "upstream_auth_failed", None, None, None, 0)]
