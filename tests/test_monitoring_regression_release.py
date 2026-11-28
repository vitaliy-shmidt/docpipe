"""Release regression (Phase M7): the customer API is identical with monitoring off,
with data collection only and with collection plus the operator interface.

Covers the HubDix paths (documents, AI, Prompt Lab, Assistant, warm-up, client
permissions) and the PDF4Fill paths (forms inspect with detection and OCR,
fill, PDF names that are not valid UTF-8, bearer auth, disabled services).
Synthetic documents and fake upstreams only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from contextlib import contextmanager

from fastapi.testclient import TestClient

from system import main as main_module
from system.config import ClientConfig, ClientServices
from tests.conftest import (
    AI_KEY,
    ASSISTANT_KEY,
    DISABLED_KEY,
    LAB_KEY,
    NO_AI_KEY,
    NO_DOCS_KEY,
    VALID_KEY,
    auth_headers,
    pdf_bytes,
)
from tests.forms_fixtures import basic_form, static_scan_like
from tests.monitoring_support import build_settings
from tests.monitoring_web_support import build_web_settings, use_fast_argon2, user_entry, write_users
from tests.test_forms_pdf_names import names_form

FORMS_KEY = "regression-forms-key"
FORMS_ONLY_KEY = "regression-forms-only-key"
VOLATILE = ("duration", "_ms", "timing", "request_id", "generated", "elapsed")


def _normalize(value):
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in sorted(value.items()) if not any(t in k for t in VOLATILE)}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _record(response) -> tuple:
    kind = response.headers.get("content-type", "").split(";")[0]
    if kind == "application/json":
        body = _normalize(response.json())
    elif kind == "application/pdf":
        body = ("pdf", response.content[:5])  # filled PDFs carry a fresh /ID; the header must be a PDF
    else:
        body = response.text
    return response.status_code, kind, body


def exercise(client, fake_stirling, fake_ollama) -> dict:
    out = {}

    def run(name, response):
        out[name] = _record(response)

    pdf = {"file": ("synthetic.pdf", pdf_bytes(), "application/pdf")}
    run("health", client.get("/api/v1/health"))
    for key in (
        VALID_KEY,
        AI_KEY,
        LAB_KEY,
        ASSISTANT_KEY,
        FORMS_KEY,
        NO_DOCS_KEY,
        DISABLED_KEY,
        "unknown-key",
    ):
        run(f"capabilities:{key}", client.get("/api/v1/capabilities", headers=auth_headers(key)))
    run("capabilities:none", client.get("/api/v1/capabilities"))
    # HubDix - documents
    run("extract", client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(VALID_KEY)))
    run(
        "extract:no_docs",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(NO_DOCS_KEY)),
    )
    run(
        "extract:disabled",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(DISABLED_KEY)),
    )
    run(
        "extract:unknown",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers("x" * 40)),
    )
    run("extract:no_auth", client.post("/api/v1/documents/extract-text", files=pdf))
    run("extract:no_file", client.post("/api/v1/documents/extract-text", headers=auth_headers(VALID_KEY)))
    fake_stirling.text = ""  # no text layer -> OCR fallback through Stirling
    run(
        "extract:ocr",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(VALID_KEY)),
    )
    fake_stirling.text = "This is extracted text from a normal text-based PDF document."
    fake_stirling.mode = "unavailable"
    fake_stirling.extract_calls = 0  # the fake applies `mode` to the first call only
    run(
        "extract:upstream_down",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(VALID_KEY)),
    )
    fake_stirling.mode = "success"
    run(
        "convert",
        client.post(
            "/api/v1/documents/convert-to-pdf",
            headers=auth_headers(VALID_KEY),
            files={"file": ("a.txt", b"synthetic", "text/plain")},
            data={"source_format": "txt"},
        ),
    )
    # HubDix - AI, Prompt Lab, Assistant
    body = {"mode": "maintenance_extraction", "text": "Synthetic report", "context": {}}
    run("analyze", client.post("/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json=body))
    run("analyze:no_ai", client.post("/api/v1/documents/analyze", headers=auth_headers(NO_AI_KEY), json=body))
    run(
        "analyze:bad_mode",
        client.post(
            "/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json={"mode": "nope", "text": "x"}
        ),
    )
    run(
        "analyze:invalid",
        client.post("/api/v1/documents/analyze", headers=auth_headers(AI_KEY), json={"x": 1}),
    )
    run(
        "warmup",
        client.post(
            "/api/v1/documents/warmup", headers=auth_headers(AI_KEY), json={"mode": "maintenance_extraction"}
        ),
    )
    run("lab:list", client.get("/api/v1/lab/prompts", headers=auth_headers(LAB_KEY)))
    run("lab:get", client.get("/api/v1/lab/prompts/maintenance_extraction", headers=auth_headers(LAB_KEY)))
    run("lab:forbidden", client.get("/api/v1/lab/prompts", headers=auth_headers(VALID_KEY)))
    run(
        "lab:analyze",
        client.post(
            "/api/v1/lab/analyze",
            headers=auth_headers(LAB_KEY),
            json={
                "mode": "maintenance_extraction",
                "prompt": "Extract {text}",
                "text": "Synthetic",
                "context": {},
            },
        ),
    )
    analysis_result = fake_ollama.result
    fake_ollama.result = {"answer": "Synthetic answer."}  # the assistant's schema
    run(
        "assistant",
        client.post(
            "/api/v1/assistant/query",
            headers=auth_headers(ASSISTANT_KEY),
            json={"question": "Synthetic question?", "context": {}, "domains": ["contracts"]},
        ),
    )
    run(
        "assistant:forbidden",
        client.post(
            "/api/v1/assistant/query", headers=auth_headers(VALID_KEY), json={"question": "x", "context": {}}
        ),
    )
    fake_ollama.result = analysis_result
    # PDF4Fill - forms
    for name, content in (("basic", basic_form()), ("names", names_form()), ("scan", static_scan_like())):
        files = {"file": ("form.pdf", content, "application/pdf")}
        run(
            f"inspect:{name}",
            client.post(
                "/api/v1/documents/forms/inspect",
                headers=auth_headers(FORMS_KEY),
                files=files,
                data={"detect": "auto", "ocr": "auto"},
            ),
        )
    run(
        "inspect:forms_disabled",
        client.post(
            "/api/v1/documents/forms/inspect",
            headers=auth_headers(VALID_KEY),
            files={"file": ("f.pdf", basic_form(), "application/pdf")},
        ),
    )
    run(
        "extract:forms_only",
        client.post("/api/v1/documents/extract-text", files=pdf, headers=auth_headers(FORMS_ONLY_KEY)),
    )
    template = names_form()
    inspected = client.post(
        "/api/v1/documents/forms/inspect",
        headers=auth_headers(FORMS_KEY),
        files={"file": ("form.pdf", template, "application/pdf")},
        data={"detect": "off"},
    )
    fields = inspected.json()["data"]["fields"]
    entries = [
        {
            "id": f["id"],
            "mode": "native",
            "type": f["type"],
            "native_ref": f["native_ref"],
            "value": f["options"][0]["value"] if f.get("options") else "Synthetic",
        }
        for f in fields
        if f["type"] in ("text", "radio", "checkbox", "combo")
    ][:4]
    manifest = {
        "schema_version": "1",
        "sha256": hashlib.sha256(template).hexdigest(),
        "fields": entries,
        "output": {"flatten": True},
    }
    run(
        "fill:names",
        client.post(
            "/api/v1/documents/forms/fill",
            headers=auth_headers(FORMS_KEY),
            files={"file": ("form.pdf", template, "application/pdf")},
            data={"manifest": json.dumps(manifest)},
        ),
    )
    run(
        "fill:wrong_hash",
        client.post(
            "/api/v1/documents/forms/fill",
            headers=auth_headers(FORMS_KEY),
            files={"file": ("form.pdf", basic_form(), "application/pdf")},
            data={"manifest": json.dumps(manifest)},
        ),
    )
    run("unmatched", client.get("/api/v1/does-not-exist"))
    return out


def _with_forms_clients(settings):
    extra = {
        "regression-forms": ClientConfig(
            client_id="regression-forms",
            enabled=True,
            api_key=FORMS_KEY,
            services=ClientServices(documents=True, forms=True),
        ),
        "regression-forms-only": ClientConfig(
            client_id="regression-forms-only",
            enabled=True,
            api_key=FORMS_ONLY_KEY,
            services=ClientServices(forms=True),
        ),
    }
    return dataclasses.replace(settings, clients={**settings.clients, **extra})


@contextmanager
def _client(monkeypatch, settings):
    monkeypatch.setattr(main_module, "load_settings", lambda: settings)
    with TestClient(main_module.app, base_url="https://testserver", client=("127.0.0.1", 50000)) as client:
        yield client


def test_customer_api_is_identical_in_all_three_modes(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    use_fast_argon2(monkeypatch)
    off = _with_forms_clients(build_settings(tmp_path / "off", enabled=False))
    collect = _with_forms_clients(build_settings(tmp_path / "collect"))
    web = _with_forms_clients(build_web_settings(tmp_path / "web"))
    web.monitoring.web.users_file.parent.mkdir(parents=True, exist_ok=True)
    write_users(web.monitoring.web.users_file, [user_entry()])
    results = {}
    for name, settings in (("off", off), ("collect", collect), ("web", web)):
        with _client(monkeypatch, settings) as client:
            stirling, ollama = fake_stirling_holder["client"], fake_ollama_holder["client"]
            results[name] = exercise(client, stirling, ollama)
            if name == "off":
                assert client.app.state.monitoring is None
                assert not (tmp_path / "off" / "mon").exists()  # no files, no threads, no routes
                assert client.get("/monitor").status_code == 404
            else:
                monitor = client.app.state.monitoring
                monitor.flush_now()
                assert monitor.store.is_open
    expected = results["off"]
    assert (
        expected["analyze"][0] == 200
        and expected["inspect:names"][0] == 200
        and expected["fill:names"][0] == 200
    )
    assert expected["extract:no_docs"][0] == 403 and expected["inspect:forms_disabled"][0] == 403
    assert expected["extract:ocr"][0] == 200 and expected["extract:upstream_down"][0] in (502, 503)
    assert (
        expected["assistant"][0] == 200 and expected["lab:analyze"][0] == 200 and expected["warmup"][0] == 200
    )
    assert expected["fill:wrong_hash"][0] == 409 and expected["extract:disabled"][0] == 401
    for name in ("collect", "web"):
        diff = {k: (expected[k], results[name][k]) for k in expected if expected[k] != results[name][k]}
        assert diff == {}, (name, diff)
