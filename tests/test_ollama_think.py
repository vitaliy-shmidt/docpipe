"""Configuration option `ollama.think` (release preparation after M7).

Absent: the `think` field is not sent - byte-identical requests to every
earlier version. true/false: sent on the primary and the repair call of a
structured generation, never with a warm-up. No retry, no other model.
"""

from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from system.config import ConfigError, load_settings
from system.errors import DocPipeError
from system.services.ollama import OllamaClient
from tests.conftest import AI_KEY, ASSISTANT_KEY, auth_headers
from tests.monitoring_support import build_settings, monitored_client
from tests.test_config import _config_with_ollama_block

SCHEMA = {
    "type": "object",
    "properties": {"foo": {"type": ["string", "null"]}},
    "required": ["foo"],
    "additionalProperties": False,
}
CALL = {"model": "test-model", "timeout_seconds": 5, "temperature": 0}


def _client(handler, **kwargs) -> OllamaClient:
    client = OllamaClient("http://ollama.test", **kwargs)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def _recording(responses):
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return responses[min(len(bodies), len(responses)) - 1]

    return handler, bodies


def _ok(text, **extra):
    return httpx.Response(200, json={"response": text, "done": True, **extra})


# --- request payload ---------------------------------------------------------------------


def test_absent_option_sends_no_think_field_anywhere():
    handler, bodies = _recording([_ok("not json"), _ok(json.dumps({"foo": "x"})), _ok("")])
    client = _client(handler)
    assert client.generate_structured("p", SCHEMA, **CALL) == {"foo": "x"}  # primary + repair
    client.warm_up(model="test-model", timeout_seconds=5)
    assert len(bodies) == 3 and all("think" not in body for body in bodies)


@pytest.mark.parametrize("value", [False, True])
def test_explicit_value_is_sent_on_primary_and_repair_call_but_not_on_warm_up(value):
    handler, bodies = _recording([_ok("not json"), _ok(json.dumps({"foo": "x"})), _ok("")])
    client = _client(handler, think=value)
    client.generate_structured("p", SCHEMA, **CALL)
    client.warm_up(model="test-model", timeout_seconds=5)
    assert [b.get("think", "absent") for b in bodies] == [value, value, "absent"]
    assert bodies[0]["think"] is value  # a JSON boolean, not a string


def test_other_payload_fields_are_unchanged_by_the_option():
    def payload(**kwargs):
        handler, bodies = _recording([_ok(json.dumps({"foo": "x"}))])
        _client(handler, keep_alive="15m", **kwargs).generate_structured("p", SCHEMA, num_ctx=4096, **CALL)
        return bodies[0]

    without, with_false = payload(), payload(think=False)
    assert with_false.pop("think") is False
    assert with_false == without


# --- answers ---------------------------------------------------------------------------------


def test_thinking_text_in_the_answer_is_ignored():
    """With think: true Ollama returns its reasoning separately; DocPipe reads only `response`."""
    handler, _ = _recording([_ok(json.dumps({"foo": "x"}), thinking="SECRET-REASONING", eval_count=5)])
    client = _client(handler, think=True)
    timing: dict = {}
    assert client.generate_structured("p", SCHEMA, timing=timing, **CALL) == {"foo": "x"}
    assert "SECRET-REASONING" not in json.dumps(timing)


def test_older_ollama_answer_without_timing_or_thinking_fields():
    handler, bodies = _recording([httpx.Response(200, json={"response": json.dumps({"foo": "old"})})])
    client = _client(handler, think=False)
    assert client.generate_structured("p", SCHEMA, **CALL) == {"foo": "old"}
    assert len(bodies) == 1


def test_model_without_thinking_support_fails_once_without_retry_or_other_model():
    error = httpx.Response(400, json={"error": '"test-model" does not support thinking'})
    handler, bodies = _recording([error])
    client = _client(handler, think=True)
    with pytest.raises(DocPipeError) as exc:
        client.generate_structured("p", SCHEMA, **CALL)
    assert exc.value.code == "ai_processing_failed"
    assert len(bodies) == 1 and bodies[0]["model"] == "test-model"  # no silent retry, no other model


def test_warm_up_works_with_think_true_on_any_model():
    handler, bodies = _recording([_ok("")])
    _client(handler, think=True).warm_up(model="no-thinking-model", timeout_seconds=5)
    assert "think" not in bodies[0]


# --- configuration -----------------------------------------------------------------------


def _load(tmp_path, monkeypatch, block: str):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(_config_with_ollama_block(block), encoding="utf-8")
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    return load_settings()


@pytest.mark.parametrize(
    "block, expected",
    [
        ("", None),
        ("  think: null", None),
        ("  think: false", False),
        ("  think: true", True),
        ("  think: False", False),
    ],
)
def test_config_values(tmp_path, monkeypatch, block, expected):
    assert _load(tmp_path, monkeypatch, block).ollama.think is expected


@pytest.mark.parametrize("value", ['"false"', '"true"', "0", "1", '"no"', "[]", '"low"'])
def test_invalid_config_values_are_rejected(tmp_path, monkeypatch, value):
    with pytest.raises(ConfigError, match="ollama.think"):
        _load(tmp_path, monkeypatch, f"  think: {value}")


def test_other_ollama_settings_keep_their_defaults(tmp_path, monkeypatch):
    settings = _load(tmp_path, monkeypatch, "  think: false")
    assert settings.ollama.keep_alive == "5m" and settings.ollama.base_url == "http://ollama:11434"


# --- application wiring, monitoring on and off ----------------------------------------------


@pytest.mark.parametrize("think", [None, False, True])
@pytest.mark.parametrize("monitoring", [False, True])
def test_the_app_hands_the_option_to_its_ollama_client(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path, think, monitoring
):
    settings = build_settings(tmp_path, enabled=monitoring)
    settings = dataclasses.replace(settings, ollama=dataclasses.replace(settings.ollama, think=think))
    with monitored_client(monkeypatch, tmp_path, settings=settings) as client:
        fake = fake_ollama_holder["client"]
        assert fake.think is think
        analyze = client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "maintenance_extraction", "text": "Synthetic", "context": {}},
        )
        assert analyze.status_code == 200 and fake.calls == 1  # exactly one AI call, contract unchanged
        assert analyze.json()["ok"] is True
        fake.result = {"answer": "Synthetic."}
        assistant = client.post(
            "/api/v1/assistant/query",
            headers=auth_headers(ASSISTANT_KEY),
            json={"question": "x?", "context": {}, "domains": ["contracts"]},
        )
        assert assistant.status_code == 200 and fake.calls == 2


def test_api_responses_are_identical_for_every_think_value(
    fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path
):
    from tests.test_monitoring_regression_release import _normalize

    results = {}
    for think in (None, False, True):
        settings = build_settings(tmp_path / str(think), enabled=False)
        settings = dataclasses.replace(settings, ollama=dataclasses.replace(settings.ollama, think=think))
        with monitored_client(monkeypatch, tmp_path, settings=settings) as client:
            fake = fake_ollama_holder["client"]
            analyze = client.post(
                "/api/v1/documents/analyze",
                headers=auth_headers(AI_KEY),
                json={"mode": "maintenance_extraction", "text": "Synthetic", "context": {}},
            )
            fake.result = {"answer": "Synthetic."}
            assistant = client.post(
                "/api/v1/assistant/query",
                headers=auth_headers(ASSISTANT_KEY),
                json={"question": "x?", "context": {}, "domains": ["contracts"]},
            )
            warmup = client.post(
                "/api/v1/documents/warmup",
                headers=auth_headers(AI_KEY),
                json={"mode": "maintenance_extraction"},
            )
            results[think] = [(r.status_code, _normalize(r.json())) for r in (analyze, assistant, warmup)]
    assert results[None][0][0] == 200
    assert results[None] == results[False] == results[True]
