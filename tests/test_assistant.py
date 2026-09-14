from __future__ import annotations

from tests.conftest import ASSISTANT_KEY, LIGHT_MODEL, STANDARD_MODEL, VALID_KEY, auth_headers

QUERY_URL = "/api/v1/assistant/query"


def _query(client, question="Welche Wartungen sind überfällig?", context=None, headers=None):
    return client.post(
        QUERY_URL,
        json={"question": question, "context": context if context is not None else {}},
        headers=auth_headers(ASSISTANT_KEY) if headers is None else headers,
    )


def test_assistant_missing_auth(client):
    response = _query(client, headers={})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_assistant_disabled_for_client_without_permission(client):
    response = _query(client, headers=auth_headers(VALID_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "assistant_disabled"


def test_assistant_empty_question_rejected(client):
    response = _query(client, question="   ")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_assistant_question_too_long_rejected(client):
    response = _query(client, question="x" * 5001)
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_assistant_context_too_large_rejected(client):
    huge_context = {"data": "x" * 100_001}
    response = _query(client, context=huge_context)
    assert response.status_code == 413
    assert response.json()["code"] == "input_too_large"


def test_assistant_rejects_unknown_fields(client):
    response = client.post(
        QUERY_URL,
        json={"question": "Was muss ich wissen?", "context": {}, "model": "sneaky-model"},
        headers=auth_headers(ASSISTANT_KEY),
    )
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_request"


def test_assistant_routes_maintenance_question(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "2 Wartungen sind überfällig."}
    response = _query(client, question="Welche Wartungen sind überfällig?")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["assistant_mode"] == "maintenance_question"
    assert body["matched_rule"] == "maintenance_keywords"
    assert body["model_profile"] == "light"
    assert body["model"] == LIGHT_MODEL
    assert body["prompt_version"] == "v1"
    assert body["answer"] == "2 Wartungen sind überfällig."


def test_assistant_routes_hotel_health_summary_to_standard_profile(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "Insgesamt stabil."}
    response = _query(client, question="Wie steht mein Hotel technisch da?")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["assistant_mode"] == "hotel_health_summary"
    assert body["model_profile"] == "standard"
    assert body["model"] == STANDARD_MODEL


def test_assistant_fallback_routes_to_general_mode(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "..."}
    response = _query(client, question="Was muss ich wissen?")
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["assistant_mode"] == "general_hotel_question"
    assert body["matched_rule"] == "fallback"


def test_assistant_context_reaches_prompt_but_request_context_is_not_mutated(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "..."}
    original_context = {"maintenance": {"overdue": 2}}
    response = _query(client, context=dict(original_context))
    assert response.status_code == 200
    assert '"overdue": 2' in fake.last_prompt
    # The request payload itself must never be mutated by routing/prompting.
    assert original_context == {"maintenance": {"overdue": 2}}


def test_assistant_grounding_rules_present_in_prompt(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "..."}
    _query(client, question="Welche Wartungen sind überfällig?")
    assert "ONLY the supplied CONTEXT" in fake.last_prompt
    assert "Never invent" in fake.last_prompt


def test_assistant_ollama_unavailable(client):
    client.fake_ollama["client"].mode = "unavailable"
    response = _query(client)
    assert response.status_code == 502
    assert response.json()["code"] == "ai_unavailable"


def test_assistant_ollama_timeout(client):
    client.fake_ollama["client"].mode = "timeout"
    response = _query(client)
    assert response.status_code == 504
    assert response.json()["code"] == "ai_timeout"


def test_assistant_ollama_invalid_response(client):
    client.fake_ollama["client"].mode = "invalid_response"
    response = _query(client)
    assert response.status_code == 502
    assert response.json()["code"] == "ai_invalid_response"


def test_assistant_error_response_has_no_secrets_or_question(client):
    client.fake_ollama["client"].mode = "unavailable"
    response = _query(client, question="CONFIDENTIAL-QUESTION-XYZ")
    body = response.json()
    assert set(body.keys()) == {"ok", "code", "message"}
    assert "CONFIDENTIAL-QUESTION-XYZ" not in response.text


def test_assistant_success_response_does_not_echo_question_or_raw_context(client):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "..."}
    response = _query(client, question="UNIQUE-QUESTION-TOKEN", context={"secret_field": "SECRET-VALUE"})
    assert "UNIQUE-QUESTION-TOKEN" not in response.text
    assert "SECRET-VALUE" not in response.text


def test_capabilities_reports_assistant_modes_for_assistant_client(client):
    response = client.get("/api/v1/capabilities", headers=auth_headers(ASSISTANT_KEY))
    body = response.json()
    assert body["services"]["assistant"] is True
    assert "assistant" in body["features"]
    assert set(body["assistant_modes"]) == {
        "hotel_health_summary",
        "maintenance_question",
        "inspection_question",
        "contract_question",
        "document_question",
        "general_hotel_question",
    }


def test_capabilities_hides_assistant_modes_for_non_assistant_client(client):
    response = client.get("/api/v1/capabilities", headers=auth_headers(VALID_KEY))
    body = response.json()
    assert body["services"]["assistant"] is False
    assert "assistant" not in body["features"]
    assert body["assistant_modes"] is None
