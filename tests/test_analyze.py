from __future__ import annotations

import jsonschema

from system.ai.modes import MODES
from tests.conftest import AI_KEY, NO_AI_KEY, SAMPLE_MAINTENANCE_RESULT, VALID_KEY, auth_headers

ANALYZE_URL = "/api/v1/documents/analyze"

MAINTENANCE_SAMPLE_TEXT = (
    "WARTUNGSBERICHT\nAnlage: Aufzug 1\nDienstleister: KONE GmbH\n"
    "Datum: 12.09.2026\nErgebnis: ohne Beanstandung\n"
)

INSPECTION_SAMPLE_TEXT = (
    "PRUEFBERICHT\nPruefungsart: Aufzugspruefung\nDatum: 01.03.2026\n"
    "Pruefer: Hans Mueller\nErgebnis: bestanden, keine Maengel\n"
    "Zertifikat-Nr.: ABC-123\n"
)


def _analyze(client, mode="maintenance_extraction", context=None, text=MAINTENANCE_SAMPLE_TEXT, headers=None):
    return client.post(
        ANALYZE_URL,
        json={"mode": mode, "context": context or {}, "text": text},
        headers=auth_headers(AI_KEY) if headers is None else headers,
    )


def test_analyze_missing_auth(client):
    response = _analyze(client, headers={})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_analyze_ai_disabled_for_client(client):
    response = _analyze(client, headers=auth_headers(NO_AI_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "ai_disabled"


def test_analyze_documents_only_client_cannot_use_ai(client):
    # VALID_KEY belongs to a documents-only client (services.ai defaults False).
    response = _analyze(client, headers=auth_headers(VALID_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "ai_disabled"


def test_analyze_unknown_mode(client):
    response = _analyze(client, mode="does_not_exist")
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_mode"


def test_analyze_missing_text(client):
    response = client.post(
        ANALYZE_URL,
        json={"mode": "maintenance_extraction"},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_analyze_invalid_context_type(client):
    response = client.post(
        ANALYZE_URL,
        json={"mode": "maintenance_extraction", "context": "not-an-object", "text": "x"},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_analyze_input_too_large(client):
    mode = MODES["maintenance_extraction"]
    too_long = "x" * (mode.max_input_length + 1)
    response = _analyze(client, text=too_long)
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_analyze_success(client):
    response = _analyze(client, context={"known_vendor": "KONE GmbH"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["mode"] == "maintenance_extraction"
    assert body["data"]["prompt_version"] == "v1"
    assert body["data"]["model"] == "test-model"
    assert body["data"]["result"] == SAMPLE_MAINTENANCE_RESULT


def test_analyze_context_reaches_prompt_but_is_marked_as_hint_only(client):
    _analyze(client, context={"known_vendor": "KONE GmbH"})
    prompt = client.fake_ollama["client"].last_prompt
    assert "KONE GmbH" in prompt
    assert "Use context ONLY to resolve" in prompt or "ONLY to resolve" in prompt


def test_analyze_ollama_unavailable(client):
    client.fake_ollama["client"].mode = "unavailable"
    response = _analyze(client)
    assert response.status_code == 502
    assert response.json()["code"] == "ai_unavailable"


def test_analyze_ollama_timeout(client):
    client.fake_ollama["client"].mode = "timeout"
    response = _analyze(client)
    assert response.status_code == 504
    assert response.json()["code"] == "ai_timeout"


def test_analyze_ollama_invalid_response(client):
    client.fake_ollama["client"].mode = "invalid_response"
    response = _analyze(client)
    assert response.status_code == 502
    assert response.json()["code"] == "ai_invalid_response"


def test_analyze_ollama_processing_failed(client):
    client.fake_ollama["client"].mode = "processing_failed"
    response = _analyze(client)
    assert response.status_code == 500
    assert response.json()["code"] == "ai_processing_failed"


def test_analyze_error_response_has_no_secrets_or_prompt(client):
    client.fake_ollama["client"].mode = "unavailable"
    response = _analyze(client, text="CONFIDENTIAL-DOCUMENT-BODY-XYZ")
    body = response.json()
    assert set(body.keys()) == {"ok", "code", "message"}
    assert "CONFIDENTIAL-DOCUMENT-BODY-XYZ" not in response.text
    assert "test-model" not in response.text


def test_analyze_success_response_does_not_echo_document_text(client):
    response = _analyze(client, text=MAINTENANCE_SAMPLE_TEXT)
    assert MAINTENANCE_SAMPLE_TEXT not in response.text


def test_maintenance_extraction_mock_output_matches_schema(client):
    response = _analyze(client, mode="maintenance_extraction", text=MAINTENANCE_SAMPLE_TEXT)
    assert response.status_code == 200
    result = response.json()["data"]["result"]
    jsonschema.validate(result, MODES["maintenance_extraction"].response_schema)


def test_inspection_extraction_mock_output_matches_schema(client):
    inspection_result = {
        "inspection_type": "Aufzugspruefung",
        "inspection_date": "2026-03-01",
        "next_due_date": None,
        "vendor_name": None,
        "inspector": "Hans Mueller",
        "result": "bestanden",
        "defects_found": False,
        "certificate_number": "ABC-123",
        "notes": None,
    }
    client.fake_ollama["client"].result = inspection_result
    response = _analyze(client, mode="inspection_extraction", text=INSPECTION_SAMPLE_TEXT)
    assert response.status_code == 200
    result = response.json()["data"]["result"]
    assert result == inspection_result
    jsonschema.validate(result, MODES["inspection_extraction"].response_schema)


def test_capabilities_reports_ai_modes_for_ai_client(client):
    response = client.get("/api/v1/capabilities", headers=auth_headers(AI_KEY))
    body = response.json()
    assert body["services"]["ai"] is True
    assert "analyze" in body["features"]
    assert set(body["ai_modes"]) == {"maintenance_extraction", "inspection_extraction"}


def test_capabilities_hides_ai_modes_for_non_ai_client(client):
    response = client.get("/api/v1/capabilities", headers=auth_headers(NO_AI_KEY))
    body = response.json()
    assert body["services"]["ai"] is False
    assert "analyze" not in body["features"]
    assert body["ai_modes"] is None
