from __future__ import annotations

import logging

from tests.conftest import ASSISTANT_KEY, LIGHT_MODEL, STANDARD_MODEL, VALID_KEY, auth_headers

QUERY_URL = "/api/v1/assistant/query"
WARMUP_URL = "/api/v1/assistant/warmup"


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


# --- Timing Metrics pass (task §11/§14/§33) ---------------------------------


def test_assistant_success_log_line_has_timing_and_no_content(client, caplog):
    fake = client.fake_ollama["client"]
    fake.result = {"answer": "..."}
    with caplog.at_level(logging.INFO, logger="docpipe.assistant"):
        response = _query(client, question="UNIQUE-QUESTION-TOKEN", context={"secret_field": "SECRET-VALUE"})
    assert response.status_code == 200

    [record] = [r for r in caplog.records if r.name == "docpipe.assistant"]
    message = record.getMessage()

    assert "status=success" in message
    assert "assistant_mode=" in message
    assert "model=" in message
    assert "model_profile=" in message
    assert "prompt_version=" in message
    assert "question_chars=" in message
    assert "context_chars=" in message
    assert "ollama_duration_ms=" in message
    assert "total_duration_ms=" in message
    # Task §14/§33: never the question, the context content, the answer, or
    # a secret - metadata (counts/mode/model/duration) only.
    assert "UNIQUE-QUESTION-TOKEN" not in message
    assert "SECRET-VALUE" not in message
    assert "..." not in message  # the fake answer text itself


def test_assistant_error_log_line_has_timing_and_status_code(client, caplog):
    fake = client.fake_ollama["client"]
    fake.mode = "timeout"
    with caplog.at_level(logging.INFO, logger="docpipe.assistant"):
        response = _query(client, question="Wie steht mein Hotel technisch da?")
    assert response.status_code == 504

    [record] = [r for r in caplog.records if r.name == "docpipe.assistant"]
    message = record.getMessage()
    assert "status=ai_timeout" in message
    assert "total_duration_ms=" in message


# --- Assistant Warm-up ------------------------------------------------------


def _warmup(client, json_body=None, headers=None):
    return client.post(
        WARMUP_URL,
        json=json_body if json_body is not None else {},
        headers=auth_headers(ASSISTANT_KEY) if headers is None else headers,
    )


def test_warmup_missing_auth(client):
    response = _warmup(client, headers={})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_warmup_disabled_for_client_without_permission(client):
    response = _warmup(client, headers=auth_headers(VALID_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "assistant_disabled"


def test_warmup_success_resolves_hotel_health_summary_model(client):
    response = _warmup(client)
    assert response.status_code == 200
    body = response.json()["data"]
    assert body["ready"] is True
    assert body["model_profile"] == "standard"
    assert body["model"] == STANDARD_MODEL
    assert isinstance(body["duration_ms"], (int, float))

    fake = client.fake_ollama["client"]
    assert fake.warmup_calls == 1
    assert fake.last_warmup_model == STANDARD_MODEL


def test_warmup_is_idempotent_across_repeated_calls(client):
    first = _warmup(client)
    second = _warmup(client)
    assert first.status_code == 200
    assert second.status_code == 200
    assert client.fake_ollama["client"].warmup_calls == 2


def test_warmup_rejects_unknown_fields(client):
    for field, value in (
        ("model", LIGHT_MODEL),
        ("prompt", "hello"),
        ("keep_alive", "1h"),
        ("hotel_id", 42),
        ("context", {}),
        ("question", "Wie steht mein Hotel da?"),
    ):
        response = _warmup(client, json_body={field: value})
        assert response.status_code == 400, f"field {field!r} should be rejected"
        assert response.json()["code"] == "invalid_request"


def test_warmup_ollama_unavailable(client):
    client.fake_ollama["client"].warmup_mode = "unavailable"
    response = _warmup(client)
    assert response.status_code == 502
    assert response.json()["code"] == "ai_unavailable"


def test_warmup_ollama_timeout(client):
    client.fake_ollama["client"].warmup_mode = "timeout"
    response = _warmup(client)
    assert response.status_code == 504
    assert response.json()["code"] == "ai_timeout"


def test_warmup_success_log_line_has_timing_and_no_content(client, caplog):
    with caplog.at_level(logging.INFO, logger="docpipe.assistant"):
        response = _warmup(client)
    assert response.status_code == 200

    records = [r for r in caplog.records if r.name == "docpipe.assistant"]
    message = records[-1].getMessage()

    assert "status=success" in message
    assert "model_profile=standard" in message
    assert f"model={STANDARD_MODEL}" in message
    assert "ollama_duration_ms=" in message
    assert "total_duration_ms=" in message
    # No question/context/hotel fields exist for warm-up in the first place -
    # this just confirms the log line never grew any (task §17/§44).
    assert "question" not in message
    assert "context" not in message
    assert "hotel" not in message
