"""POST /api/v1/documents/warmup - warm the model an extraction mode resolves to."""

from __future__ import annotations

import logging

from tests.conftest import (
    AI_KEY,
    AI_OVERRIDE_KEY,
    ASSISTANT_KEY,
    LIGHT_MODEL,
    NO_AI_KEY,
    STANDARD_MODEL,
    VALID_KEY,
    auth_headers,
)

URL = "/api/v1/documents/warmup"


def _warmup(client, body=None, headers=None):
    return client.post(
        URL,
        json={"mode": "contract_extraction"} if body is None else body,
        headers=auth_headers(AI_KEY) if headers is None else headers,
    )


def test_warmup_contract_extraction_resolves_standard_profile(client):
    response = _warmup(client)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ready"] is True
    assert data["model_profile"] == "standard"
    assert data["model"] == STANDARD_MODEL
    fake = client.fake_ollama["client"]
    assert fake.warmup_calls == 1
    assert fake.last_warmup_model == STANDARD_MODEL
    # Warm-up never runs a generate call.
    assert fake.calls == 0


def test_warmup_uses_the_same_resolution_as_analyze(client):
    # maintenance_extraction defaults to "light"; ai-override-client bumps
    # it to "standard" - warm-up must follow exactly the same resolution.
    assert _warmup(client, {"mode": "maintenance_extraction"}).json()["data"]["model"] == LIGHT_MODEL
    bumped = _warmup(client, {"mode": "maintenance_extraction"}, headers=auth_headers(AI_OVERRIDE_KEY))
    assert bumped.json()["data"]["model_profile"] == "standard"


def test_warmup_timeout_is_bounded(client):
    _warmup(client)
    assert 120 <= client.fake_ollama["client"].last_warmup_timeout_seconds <= 180


def test_warmup_requires_auth_and_ai_service(client):
    assert _warmup(client, headers={}).status_code == 401
    for key in (NO_AI_KEY, VALID_KEY):
        response = _warmup(client, headers=auth_headers(key))
        assert response.status_code == 403
        assert response.json()["code"] == "ai_disabled"
    assert client.fake_ollama["client"].warmup_calls == 0


def test_warmup_assistant_permission_alone_is_not_needed_nor_sufficient(client):
    # assistant-client also has ai=True, so it may warm extraction modes; the
    # extraction warm-up is gated by `ai`, not by `assistant`.
    assert _warmup(client, headers=auth_headers(ASSISTANT_KEY)).status_code == 200


def test_warmup_unknown_mode(client):
    for mode in ("does_not_exist", "contract_question", "hotel_health_summary"):
        response = _warmup(client, {"mode": mode})
        assert response.status_code == 400
        assert response.json()["code"] == "unknown_mode"


def test_warmup_rejects_client_model_choice(client):
    for field, value in (
        ("model", "huge"),
        ("model_profile", "heavy"),
        ("keep_alive", "1h"),
        ("text", "x"),
        ("prompt", "x"),
    ):
        response = _warmup(client, {"mode": "contract_extraction", field: value})
        assert response.status_code == 400, field
        assert response.json()["code"] == "invalid_request"
    assert client.fake_ollama["client"].warmup_calls == 0


def test_warmup_errors_map_like_analyze(client):
    fake = client.fake_ollama["client"]
    fake.warmup_mode = "unavailable"
    assert _warmup(client).json()["code"] == "ai_unavailable"
    fake.warmup_mode = "timeout"
    response = _warmup(client)
    assert response.status_code == 504
    assert response.json()["code"] == "ai_timeout"


def test_warmup_log_is_metadata_only(client, caplog):
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        _warmup(client)
    [record] = [r for r in caplog.records if r.name == "docpipe.ai"]
    message = record.getMessage()
    assert "action=warmup" in message
    assert "mode=contract_extraction" in message
    assert "model_profile=standard" in message
    assert "status=success" in message
