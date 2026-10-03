"""contract_extraction mode - registry, prompt/schema, analyze, Prompt Lab.

Uses the same fakes as test_analyze.py/test_lab.py (conftest.py) for the
route-level tests and the real OllamaClient over httpx.MockTransport (like
test_ollama_client.py) for the validation/repair tests, but always against
the real contract_extraction schema - nothing here is mode-specific
pipeline code, the point is to prove the new mode rides the existing one.
"""

from __future__ import annotations

import json
import logging
import re

import httpx
import jsonschema
import pytest

from system.ai.modes import MODES, PROMPTS_DIR, get_mode
from system.ai.prompting import build_prompt
from system.ai.resolver import resolve_model_profile
from system.errors import DocPipeError
from system.services.ollama import OllamaClient
from tests.conftest import (
    AI_KEY,
    DISABLED_KEY,
    LAB_KEY,
    NO_AI_KEY,
    STANDARD_MODEL,
    VALID_KEY,
    _build_test_settings,
    auth_headers,
)

MODE = "contract_extraction"
ANALYZE_URL = "/api/v1/documents/analyze"
LAB_ANALYZE_URL = "/api/v1/lab/analyze"
LAB_LIST_URL = "/api/v1/lab/prompts"

# --- Realistic, synthetic sample texts (no real persons/companies) --------

SIMPLE_CONTRACT_TEXT = (
    "WARTUNGSVERTRAG Nr. WV-2026-0815\n"
    "zwischen Hotel Beispielhof GmbH (Auftraggeber)\n"
    "und Musterlift Aufzugsservice GmbH (Auftragnehmer)\n"
    "Gegenstand: Wartung der Aufzugsanlagen\n"
    "Vertragsbeginn: 01.01.2026\nVertragsende: 31.12.2027\n"
    "Vergütung: 1.200,00 EUR monatlich\n"
)

SIMPLE_CONTRACT_RESULT = {
    "vendor_name": "Musterlift Aufzugsservice GmbH",
    "contact_person": None,
    "contract_type": "Wartung der Aufzugsanlagen",
    "contract_number": "WV-2026-0815",
    "service_description": None,
    "start_date": "2026-01-01",
    "end_date": "2027-12-31",
    "is_open_ended": False,
    "notice_period": None,
    "notice_period_value": None,
    "notice_period_unit": None,
    "renewal_terms": None,
    "auto_renewal": None,
    "renewal_period_value": None,
    "renewal_period_unit": None,
    "amount": 1200.0,
    "currency": "EUR",
    "payment_interval": "monthly",
    "notes": None,
}

NOTICE_CLAUSE_TEXT = (
    "§ 5 Laufzeit und Kündigung\n"
    "Der Vertrag beginnt am 01.04.2025 und läuft zunächst 24 Monate. Er verlängert sich "
    "jeweils um weitere 12 Monate, sofern er nicht mit einer Frist von drei Monaten zum Ende "
    "der jeweiligen Laufzeit schriftlich gekündigt wird. Das Recht zur außerordentlichen "
    "Kündigung aus wichtigem Grund bleibt unberührt.\n"
)

# What a correct, non-hallucinating answer looks like: the start date is
# explicit; the END date is NOT a printed date (only "24 Monate"), so it
# must stay null - never 2027-03-31 computed by the model.
NOTICE_CLAUSE_RESULT = {
    **{key: None for key in SIMPLE_CONTRACT_RESULT},
    "start_date": "2025-04-01",
    "notice_period": "3 Monate zum Ende der jeweiligen Laufzeit, schriftlich",
    "notice_period_value": 3,
    "notice_period_unit": "months",
    "renewal_terms": "verlängert sich jeweils um weitere 12 Monate, sofern nicht gekündigt",
    "auto_renewal": True,
    "renewal_period_value": 12,
    "renewal_period_unit": "months",
}

MISSING_DATA_TEXT = "Anlage 2 zum Vertrag\nLeistungsverzeichnis: Reinigung der Glasflächen\n"
MISSING_DATA_RESULT = {
    **{key: None for key in SIMPLE_CONTRACT_RESULT},
    "service_description": "Reinigung der Glasflächen",
}

CONTRADICTORY_TEXT = "Vertragslaufzeit bis 31.12.2026.\n...\n§ 9 Der Vertrag endet am 30.06.2027.\n"
CONTRADICTORY_RESULT = {
    **{key: None for key in SIMPLE_CONTRACT_RESULT},
    "notes": "Widersprüchliche Vertragsenden: 31.12.2026 und 30.06.2027.",
}


def _analyze(client, text=SIMPLE_CONTRACT_TEXT, context=None, headers=None, mode=MODE):
    return client.post(
        ANALYZE_URL,
        json={"mode": mode, "context": context or {}, "text": text},
        headers=auth_headers(AI_KEY) if headers is None else headers,
    )


# --- Registry / prompt / schema -------------------------------------------


def test_mode_is_registered_with_standard_profile():
    mode = get_mode(MODE)
    assert mode is not None
    assert mode.default_prompt_version == "v1"
    assert mode.model_profile == "standard"
    assert mode.max_input_length > 0


def test_contract_extraction_is_not_the_assistant_contract_question_mode():
    # Different semantics (extract facts vs. answer a question) - the
    # assistant mode must not be reachable as an extraction mode.
    assert get_mode("contract_question") is None


def test_prompt_file_loads_and_renders():
    template = (PROMPTS_DIR / MODE / "v1.txt").read_text(encoding="utf-8")
    rendered = build_prompt(template, {"hotel_name": "Hotel Beispielhof"}, "DOC-TEXT-123")
    assert "DOC-TEXT-123" in rendered
    assert "hotel_name: Hotel Beispielhof" in rendered


def test_prompt_contains_hallucination_guards():
    template = (PROMPTS_DIR / MODE / "v1.txt").read_text(encoding="utf-8")
    for phrase in (
        "Use context ONLY to resolve",
        "Never invent a contract term",
        "Never compute a date",
        "Do not give a legal interpretation",
        "Never assume that the contract is currently active",
        "never convert between net and gross",
        "contradicts itself",
        "Never output any identifier, database ID",
    ):
        assert phrase in template, phrase


def test_schema_loaded_and_strict():
    schema = MODES[MODE].response_schema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    jsonschema.Draft202012Validator.check_schema(schema)


def test_schema_contains_no_database_ids():
    schema = MODES[MODE].response_schema
    for field in schema["properties"]:
        assert not re.search(r"(^id$|_id$|_ID$|ID$)", field), field


def test_schema_every_field_is_nullable():
    schema = MODES[MODE].response_schema
    for name, definition in schema["properties"].items():
        if "enum" in definition:
            assert None in definition["enum"], name
        else:
            assert "null" in definition["type"], name


@pytest.mark.parametrize(
    "result",
    [SIMPLE_CONTRACT_RESULT, NOTICE_CLAUSE_RESULT, MISSING_DATA_RESULT, CONTRADICTORY_RESULT],
)
def test_example_results_validate(result):
    jsonschema.validate(result, MODES[MODE].response_schema)


@pytest.mark.parametrize(
    "patch",
    [
        {"start_date": "01.01.2026"},  # not ISO
        {"currency": "€"},  # not an ISO code
        {"payment_interval": "weekly"},  # not one of HubDix's intervals
        {"notice_period_unit": "quarters"},
        {"notice_period_value": 0},
        {"vendors_ID": 5},  # DB IDs are never allowed
    ],
)
def test_schema_rejects_invalid_values(patch):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**SIMPLE_CONTRACT_RESULT, **patch}, MODES[MODE].response_schema)


def test_model_resolver_uses_standard_profile_without_hardcoded_model(tmp_path):
    settings = _build_test_settings(tmp_path)
    client_cfg = settings.clients["ai-client"]
    profile_name, profile = resolve_model_profile(MODES[MODE], client_cfg, settings)
    assert profile_name == "standard"
    assert profile.model == STANDARD_MODEL


# --- /documents/analyze -----------------------------------------------------


def test_analyze_simple_contract(client):
    client.fake_ollama["client"].result = dict(SIMPLE_CONTRACT_RESULT)
    response = _analyze(client, context={"hotel_name": "Hotel Beispielhof", "document_type": "contract"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["mode"] == MODE
    assert data["model_profile"] == "standard"
    assert data["model"] == STANDARD_MODEL
    assert data["prompt_version"] == "v1"
    assert data["result"] == SIMPLE_CONTRACT_RESULT
    # The real contract schema is what gets sent to the model.
    fake = client.fake_ollama["client"]
    assert fake.last_model == STANDARD_MODEL
    assert "Musterlift Aufzugsservice GmbH" in fake.last_prompt


def test_analyze_null_fields_pass_through(client):
    client.fake_ollama["client"].result = dict(MISSING_DATA_RESULT)
    response = _analyze(client, text=MISSING_DATA_TEXT)
    assert response.status_code == 200
    result = response.json()["data"]["result"]
    assert result["vendor_name"] is None
    assert result["start_date"] is None
    assert result["amount"] is None


def test_analyze_notice_clause_and_contradiction_results_pass_through(client):
    for text, expected in (
        (NOTICE_CLAUSE_TEXT, NOTICE_CLAUSE_RESULT),
        (CONTRADICTORY_TEXT, CONTRADICTORY_RESULT),
    ):
        client.fake_ollama["client"].result = dict(expected)
        response = _analyze(client, text=text)
        assert response.status_code == 200
        assert response.json()["data"]["result"] == expected


@pytest.mark.parametrize(
    ("fake_mode", "status", "code"),
    [
        ("timeout", 504, "ai_timeout"),
        ("unavailable", 502, "ai_unavailable"),
        ("invalid_response", 502, "ai_invalid_response"),
        ("processing_failed", 500, "ai_processing_failed"),
    ],
)
def test_analyze_error_mapping(client, fake_mode, status, code):
    client.fake_ollama["client"].mode = fake_mode
    response = _analyze(client, text="CONFIDENTIAL-CONTRACT-BODY")
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert "CONFIDENTIAL-CONTRACT-BODY" not in response.text


def test_analyze_unauthorized(client):
    response = _analyze(client, headers={})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_analyze_disabled_client(client):
    response = _analyze(client, headers=auth_headers(DISABLED_KEY))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_analyze_ai_service_disabled(client):
    for key in (NO_AI_KEY, VALID_KEY):
        response = _analyze(client, headers=auth_headers(key))
        assert response.status_code == 403
        assert response.json()["code"] == "ai_disabled"
    assert client.fake_ollama["client"].calls == 0


@pytest.mark.parametrize(
    "extra",
    [{"model": "huge"}, {"model_profile": "heavy"}, {"prompt": "x"}, {"prompt_version": "v9"}],
)
def test_analyze_rejects_client_side_model_or_prompt_choice(client, extra):
    response = client.post(
        ANALYZE_URL,
        json={"mode": MODE, "text": SIMPLE_CONTRACT_TEXT, **extra},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"
    assert client.fake_ollama["client"].calls == 0


def test_analyze_input_too_large(client):
    response = _analyze(client, text="x" * (MODES[MODE].max_input_length + 1))
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_capabilities_lists_contract_extraction(client):
    body = client.get("/api/v1/capabilities", headers=auth_headers(AI_KEY)).json()
    assert MODE in body["ai_modes"]


def test_analyze_log_line_is_metadata_only(client, caplog):
    client.fake_ollama["client"].result = dict(SIMPLE_CONTRACT_RESULT)
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        response = _analyze(client, text="SECRET-CONTRACT-TEXT", context={"hotel_name": "SECRET-HOTEL"})
    assert response.status_code == 200
    [record] = [r for r in caplog.records if r.name == "docpipe.ai"]
    message = record.getMessage()
    assert f"mode={MODE}" in message
    assert "model_profile=standard" in message
    assert "prompt_version=v1" in message
    assert "input_chars=" in message
    assert "total_duration_ms=" in message
    assert "SECRET-CONTRACT-TEXT" not in message
    assert "SECRET-HOTEL" not in message


# --- Prompt Lab -------------------------------------------------------------


def test_prompt_lab_lists_contract_extraction(client):
    modes = {
        m["mode"]: m for m in client.get(LAB_LIST_URL, headers=auth_headers(LAB_KEY)).json()["data"]["modes"]
    }
    assert modes[MODE]["active_version"] == "v1"
    assert modes[MODE]["versions"] == ["v1"]


def test_prompt_lab_loads_contract_prompt(client):
    response = client.get(f"{LAB_LIST_URL}/{MODE}", headers=auth_headers(LAB_KEY))
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["version"] == "v1" and data["active"] is True
    assert "{context_block}" in data["content"] and "{text}" in data["content"]


def test_prompt_lab_draft_analyze_uses_contract_schema_and_profile(client):
    client.fake_ollama["client"].result = dict(SIMPLE_CONTRACT_RESULT)
    response = client.post(
        LAB_ANALYZE_URL,
        json={
            "mode": MODE,
            "text": SIMPLE_CONTRACT_TEXT,
            "prompt": "DRAFT\n{context_block}\n{text}",
            "context": {},
        },
        headers=auth_headers(LAB_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["prompt_source"] == "draft"
    assert data["model_profile"] == "standard"
    assert data["result"] == SIMPLE_CONTRACT_RESULT
    assert client.fake_ollama["client"].last_prompt.startswith("DRAFT")


def test_prompt_lab_save_and_activate_is_used_by_analyze(client):
    headers = auth_headers(LAB_KEY)
    saved = client.post(
        f"{LAB_LIST_URL}/{MODE}/versions",
        json={"version": "v2", "content": "CONTRACT-V2\n{context_block}\n{text}"},
        headers=headers,
    )
    assert saved.status_code == 200
    # Saving alone must not change what /documents/analyze runs.
    client.fake_ollama["client"].result = dict(SIMPLE_CONTRACT_RESULT)
    assert _analyze(client).json()["data"]["prompt_version"] == "v1"
    activated = client.post(f"{LAB_LIST_URL}/{MODE}/activate", json={"version": "v2"}, headers=headers)
    assert activated.status_code == 200
    response = _analyze(client)
    assert response.json()["data"]["prompt_version"] == "v2"
    assert client.fake_ollama["client"].last_prompt.startswith("CONTRACT-V2")


# --- Real OllamaClient: validation + repair against the contract schema ----


def _ollama_client(handler) -> OllamaClient:
    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def _ollama_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={"response": text, "done": True})


def _generate(client: OllamaClient) -> dict:
    return client.generate_structured(
        "prompt", MODES[MODE].response_schema, model="m", timeout_seconds=5, temperature=0
    )


def test_ollama_valid_structured_contract_result():
    client = _ollama_client(lambda request: _ollama_response(json.dumps(SIMPLE_CONTRACT_RESULT)))
    assert _generate(client) == SIMPLE_CONTRACT_RESULT


def test_ollama_contract_schema_is_sent_as_format():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return _ollama_response(json.dumps(MISSING_DATA_RESULT))

    _generate(_ollama_client(handler))
    assert bodies[0]["format"] == MODES[MODE].response_schema


def test_ollama_invalid_json_is_repaired():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return _ollama_response('{"vendor_name": "Musterlift"')  # truncated JSON
        return _ollama_response(json.dumps(SIMPLE_CONTRACT_RESULT))

    assert _generate(_ollama_client(handler)) == SIMPLE_CONTRACT_RESULT
    assert len(calls) == 2


def test_ollama_schema_violation_is_repaired():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            # Valid JSON, but a German date and a DB id - both schema violations.
            return _ollama_response(
                json.dumps({**SIMPLE_CONTRACT_RESULT, "start_date": "01.01.2026", "vendors_ID": 3})
            )
        return _ollama_response(json.dumps(SIMPLE_CONTRACT_RESULT))

    assert _generate(_ollama_client(handler)) == SIMPLE_CONTRACT_RESULT
    assert len(calls) == 2


def test_ollama_repair_failure_raises_invalid_response():
    def handler(request):
        return _ollama_response(json.dumps({**SIMPLE_CONTRACT_RESULT, "payment_interval": "weekly"}))

    with pytest.raises(DocPipeError) as exc:
        _generate(_ollama_client(handler))
    assert exc.value.code == "ai_invalid_response"


def test_ollama_timeout_maps_to_ai_timeout():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(DocPipeError) as exc:
        _generate(_ollama_client(handler))
    assert exc.value.code == "ai_timeout"


def test_ollama_unavailable_maps_to_ai_unavailable():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(DocPipeError) as exc:
        _generate(_ollama_client(handler))
    assert exc.value.code == "ai_unavailable"
