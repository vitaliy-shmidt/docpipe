from __future__ import annotations

import json

import httpx
import pytest

from system.errors import DocPipeError
from system.services.ollama import OllamaClient

SCHEMA = {
    "type": "object",
    "properties": {"foo": {"type": ["string", "null"]}},
    "required": ["foo"],
    "additionalProperties": False,
}

CALL_KWARGS = {"model": "test-model", "timeout_seconds": 5, "temperature": 0}


def _make_client(handler) -> OllamaClient:
    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def _generate(client: OllamaClient, prompt: str, schema: dict = SCHEMA, **overrides) -> dict:
    kwargs = {**CALL_KWARGS, **overrides}
    return client.generate_structured(prompt, schema, **kwargs)


def _ollama_response(text: str) -> httpx.Response:
    return httpx.Response(200, json={"response": text, "done": True})


def test_generate_structured_success_first_try():
    calls = []

    def handler(request):
        calls.append(request)
        return _ollama_response(json.dumps({"foo": "bar"}))

    client = _make_client(handler)
    result = _generate(client, "prompt")
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
    result = _generate(client, "prompt")
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
    result = _generate(client, "prompt")
    assert result == {"foo": "bar"}
    assert len(calls) == 2


def test_generate_structured_repair_uses_same_resolved_model_and_temperature():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return _ollama_response("not valid json")
        return _ollama_response(json.dumps({"foo": "fixed"}))

    client = _make_client(handler)
    _generate(client, "prompt", model="specific-model", timeout_seconds=42, temperature=0.3)

    assert len(bodies) == 2
    assert bodies[0]["model"] == bodies[1]["model"] == "specific-model"
    assert bodies[0]["options"]["temperature"] == bodies[1]["options"]["temperature"] == 0.3


def test_generate_structured_repair_failure_raises_ai_invalid_response():
    calls = []

    def handler(request):
        calls.append(request)
        return _ollama_response("still not json")

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        _generate(client, "prompt")
    assert excinfo.value.code == "ai_invalid_response"
    assert excinfo.value.status_code == 502
    assert len(calls) == 2  # exactly one repair attempt, no more


def test_generate_structured_connection_error_raises_ai_unavailable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        _generate(client, "prompt")
    assert excinfo.value.code == "ai_unavailable"
    assert excinfo.value.status_code == 502


def test_generate_structured_timeout_raises_ai_timeout():
    def handler(request):
        raise httpx.ReadTimeout("boom", request=request)

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        _generate(client, "prompt")
    assert excinfo.value.code == "ai_timeout"
    assert excinfo.value.status_code == 504


def test_generate_structured_non_200_raises_ai_processing_failed():
    def handler(request):
        return httpx.Response(500, json={"error": "model crashed"})

    client = _make_client(handler)
    with pytest.raises(DocPipeError) as excinfo:
        _generate(client, "prompt")
    assert excinfo.value.code == "ai_processing_failed"
    assert excinfo.value.status_code == 500


def test_generate_structured_sends_schema_as_format_and_resolved_params():
    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        return _ollama_response(json.dumps({"foo": "bar"}))

    client = _make_client(handler)
    _generate(client, "my prompt", model="light-model", timeout_seconds=60, temperature=0)

    assert captured["body"]["format"] == SCHEMA
    assert captured["body"]["options"]["temperature"] == 0
    assert captured["body"]["prompt"] == "my prompt"
    assert captured["body"]["stream"] is False
    assert captured["body"]["model"] == "light-model"


_FAKE_RESPONSE_TEXT = json.dumps({"foo": "bar"})


def test_generate_structured_passes_resolved_timeout_to_http_call():
    client = OllamaClient("http://ollama.test")
    captured_kwargs = {}

    def fake_post(url, json=None, timeout=None):
        captured_kwargs["url"] = url
        captured_kwargs["timeout"] = timeout
        return _ollama_response(_FAKE_RESPONSE_TEXT)

    client._client.post = fake_post
    _generate(client, "prompt", model="model-a", timeout_seconds=42)

    assert captured_kwargs["timeout"] == 42
    assert captured_kwargs["url"] == "http://ollama.test/api/generate"
