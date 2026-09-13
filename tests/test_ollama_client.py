from __future__ import annotations

import json

import httpx
import pytest

from system.config import OllamaSettings
from system.errors import DocPipeError
from system.services.ollama import OllamaClient

SCHEMA = {
    "type": "object",
    "properties": {"foo": {"type": ["string", "null"]}},
    "required": ["foo"],
    "additionalProperties": False,
}


def _make_client(handler) -> OllamaClient:
    settings = OllamaSettings(base_url="http://ollama.test", model="test-model", timeout_seconds=5)
    client = OllamaClient(settings)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def _ollama_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={"response": text, "done": True})


def test_generate_structured_success_first_try():
    calls = []

    def handler(request):
        calls.append(request)
        return _ollama_response(json.dumps({"foo": "bar"}))

    client = _make_client(handler)
    result = client.generate_structured("prompt", SCHEMA)
    assert result == {"foo": "bar"}
    assert len(calls) == 1


def test_generate_structured_repair_success_on_malformed_json():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return _ollama_response("not valid json{{{")
        return _ollama_response(json.dumps({"foo": "fixed"}))

    client = _make_client(handler)
    result = client.generate_structured("prompt", SCHEMA)
    assert result == {"foo": "fixed"}
    assert len(calls) == 2


def test_generate_structured_repair_success_on_schema_mismatch():
    calls = []

    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            # valid JSON, but violates the schema (extra unexpected key)
            return _ollama_response(json.dumps({"foo": "bar", "unexpected": 1}))
        return _ollama_response(json.dumps({"foo": "bar"}))

    client = _make_client(handler)
    result = client.generate_structured("prompt", SCHEMA)
    assert result == {"foo": "bar"}
    assert len(calls) == 2


def test_generate_structured_repair_failure_raises_ai_invalid_response():
    calls = []

    def handler(request):
        calls.append(request)
        return _ollama_response("still not json")

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        client.generate_structured("prompt", SCHEMA)
    assert excinfo.value.code == "ai_invalid_response"
    assert excinfo.value.status_code == 502
    assert len(calls) == 2  # exactly one repair attempt, no more


def test_generate_structured_connection_error_raises_ai_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        client.generate_structured("prompt", SCHEMA)
    assert excinfo.value.code == "ai_unavailable"
    assert excinfo.value.status_code == 502


def test_generate_structured_timeout_raises_ai_timeout():
    def handler(request):
        raise httpx.ReadTimeout("boom", request=request)

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        client.generate_structured("prompt", SCHEMA)
    assert excinfo.value.code == "ai_timeout"
    assert excinfo.value.status_code == 504


def test_generate_structured_non_200_raises_ai_processing_failed():
    def handler(request):
        return httpx.Response(500, json={"error": "model crashed"})

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        client.generate_structured("prompt", SCHEMA)
    assert excinfo.value.code == "ai_processing_failed"
    assert excinfo.value.status_code == 500


def test_generate_structured_sends_schema_as_format_and_zero_temperature():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return _ollama_response(json.dumps({"foo": "bar"}))

    client = _make_client(handler)
    client.generate_structured("my prompt", SCHEMA)
    assert captured["body"]["format"] == SCHEMA
    assert captured["body"]["options"]["temperature"] == 0
    assert captured["body"]["prompt"] == "my prompt"
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "test-model"
