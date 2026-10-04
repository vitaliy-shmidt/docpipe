"""Contract text optimization end to end: the shared extraction runner
(direct/relevance/chunked, timeout fallback, deadline), the analyze and Lab
routes (diagnostics, logging, unchanged response contract), num_ctx wiring
through OllamaClient/warm-up, and the text_optimization config section.

Ollama is always faked/mocked; texts are synthetic (contract_samples.py).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import time

import httpx
import jsonschema
import pytest

from system import main as main_module
from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES
from system.ai.prompting import build_prompt
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.settings import TextOptimizationSettings, default_text_optimization
from system.config import ConfigError, ModelProfile, load_settings
from system.errors import DocPipeError
from system.services.ollama import OllamaClient
from tests.conftest import AI_KEY, LAB_KEY, _build_test_settings, auth_headers
from tests.contract_samples import (
    HEAD,
    PRICE_TABLE,
    RELEVANT_SNIPPETS,
    TERM,
    build_contract,
    filler_section,
)

CONTRACT = MODES["contract_extraction"]
MAINTENANCE = MODES["maintenance_extraction"]
TEMPLATE = open("system/prompts/contract_extraction/v1.txt", encoding="utf-8").read()
PROFILE = ModelProfile(provider="ollama", model="m", timeout_seconds=100, temperature=0)
EMPTY_RESULT = {field: None for field in CONTRACT.response_schema["properties"]}


class ScriptedOllama:
    """generate_structured() double that records every call and answers
    from a script: a dict result, a DocPipeError to raise, or a callable
    (prompt -> dict)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def generate_structured(self, prompt, schema, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        timing = kwargs.get("timing")
        if timing is not None:
            timing["ollama_primary_duration_ms"] = 10.0
            timing["ollama_duration_ms"] = 10.0
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        return step(prompt) if callable(step) else dict(step)


def _run(text, ollama, *, mode=CONTRACT, profile=PROFILE, settings=None, observed=None):
    return run_extraction(
        mode=mode,
        prompt_template=TEMPLATE,
        context={"document_type": "contract"},
        text=text,
        profile=profile,
        ollama_client=ollama,
        optimization_settings=settings if settings is not None else default_text_optimization(),
        observed=observed,
    )


def _timeout() -> DocPipeError:
    return DocPipeError("ai_timeout", "AI analysis timed out.")


def _document_text(prompt: str) -> str:
    # The v1 template ends with "{text}" plus a final newline.
    return prompt.split("DOCUMENT TEXT:\n", 1)[1].removesuffix("\n")


def _many_clauses_contract(copies: int = 25) -> str:
    parts = [build_contract(4000)]
    for i in range(copies):
        parts.append(
            f"§ {20 + i} Preisanpassung Nr. {i}\nDie Vergütung für Paket {i} beträgt {100 + i},00 € "
            f"quartalsweise.\n\n§ {60 + i} Sonderkündigung Nr. {i}\nKündigung von Paket {i} mit einer "
            f"Frist von {i + 1} Wochen; die Laufzeit verlängert sich sonst um {i + 1} Monate."
        )
        parts.append(filler_section(100 + i, 1200))
    return "\n\n".join(parts)


# --- Runner: strategies --------------------------------------------------


def test_small_contract_one_call_with_complete_normalized_text_and_full_timeout():
    raw = f"{HEAD}\n\n\n\n{PRICE_TABLE}\n\n{TERM}   \n"
    ollama = ScriptedOllama(EMPTY_RESULT)
    outcome = _run(raw, ollama)
    assert len(ollama.calls) == 1
    assert _document_text(ollama.calls[0]["prompt"]) == normalize_text(raw)
    assert ollama.calls[0]["timeout_seconds"] == PROFILE.timeout_seconds
    assert outcome.optimization["strategy"] == "direct"
    assert outcome.optimization["ai_calls"] == 1


def test_long_contract_one_call_with_reduced_text():
    raw = build_contract(17763, pages=6, dense=True)
    ollama = ScriptedOllama(EMPTY_RESULT)
    outcome = _run(raw, ollama)
    stats = outcome.optimization
    assert len(ollama.calls) == 1
    assert stats["strategy"] == "relevance"
    assert stats["raw_chars"] == len(raw)
    assert stats["optimized_chars"] < stats["normalized_chars"]
    sent = _document_text(ollama.calls[0]["prompt"])
    assert len(sent) == stats["optimized_chars"]
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in sent, snippet


def test_chunked_contract_runs_one_call_per_group_with_same_model_and_template():
    raw = _many_clauses_contract()
    outcome = _run(raw, ScriptedOllama(EMPTY_RESULT))
    assert outcome.optimization["strategy"] == "chunked"

    ollama = ScriptedOllama(EMPTY_RESULT)
    outcome = _run(raw, ollama)
    stats = outcome.optimization
    assert 2 <= stats["ai_calls"] == stats["chunk_count"] == len(ollama.calls) <= 3
    prefix = build_prompt(TEMPLATE, {"document_type": "contract"}, "").removesuffix("\n")
    for call in ollama.calls:
        assert call["model"] == PROFILE.model
        assert call["prompt"].startswith(prefix)
        assert len(_document_text(call["prompt"])) < stats["normalized_chars"] / 2
    jsonschema.validate(outcome.result, CONTRACT.response_schema)


def test_chunked_results_are_merged_by_field_group():
    # Chunk calls run in group order: basis, term, finance. Each answer also
    # carries a "guess" for a field another group owns - it must be ignored.
    basis = {**EMPTY_RESULT, "vendor_name": "Musterlift Aufzugsservice GmbH", "start_date": "1999-01-01"}
    term = {**EMPTY_RESULT, "start_date": "2026-01-01", "notice_period": "3 Monate", "amount": 1.0}
    finance = {**EMPTY_RESULT, "amount": 450.0, "currency": "EUR", "payment_interval": "monthly"}
    outcome = _run(_many_clauses_contract(), ScriptedOllama(basis, term, finance))
    result = outcome.result
    assert outcome.optimization["strategy"] == "chunked"
    assert result["vendor_name"] == "Musterlift Aufzugsservice GmbH"
    assert result["start_date"] == "2026-01-01"  # term chunk owns it, basis guess ignored
    assert result["amount"] == 450.0 and result["currency"] == "EUR"
    jsonschema.validate(result, CONTRACT.response_schema)


def test_chunked_call_failure_returns_a_clean_error_without_further_calls():
    ollama = ScriptedOllama(EMPTY_RESULT, _timeout(), EMPTY_RESULT)
    observed: dict = {}
    with pytest.raises(DocPipeError) as exc_info:
        _run(_many_clauses_contract(), ollama, observed=observed)
    assert exc_info.value.code == "ai_timeout"
    assert len(ollama.calls) == 2
    assert observed["optimization"]["strategy"] == "chunked"
    assert observed["optimization"]["ai_calls"] == 2


def test_all_calls_share_one_deadline_bounded_by_the_profile_timeout():
    started = time.monotonic()
    ollama = ScriptedOllama(EMPTY_RESULT)
    _run(_many_clauses_contract(), ollama)
    deadlines = {call["deadline"] for call in ollama.calls}
    assert len(deadlines) == 1
    deadline = deadlines.pop()
    assert started + PROFILE.timeout_seconds - 1 <= deadline <= time.monotonic() + PROFILE.timeout_seconds


# --- Runner: timeout fallback ----------------------------------------------


def test_timeout_fallback_runs_once_with_smaller_text_and_remaining_budget():
    raw = build_contract(17763, pages=6, dense=True)
    ollama = ScriptedOllama(_timeout(), EMPTY_RESULT)
    outcome = _run(raw, ollama)
    assert len(ollama.calls) == 2
    first, second = ollama.calls
    assert first["timeout_seconds"] == pytest.approx(PROFILE.timeout_seconds * 0.65)
    assert 15 < second["timeout_seconds"] <= PROFILE.timeout_seconds
    assert len(_document_text(second["prompt"])) <= len(_document_text(first["prompt"])) / 2
    assert "WV-2026-0815" in second["prompt"]
    assert outcome.optimization["fallback_used"] is True
    assert outcome.optimization["ai_calls"] == 2


def test_timeout_fallback_failure_returns_ai_timeout_after_exactly_two_calls():
    ollama = ScriptedOllama(_timeout(), _timeout(), EMPTY_RESULT)
    with pytest.raises(DocPipeError) as exc_info:
        _run(build_contract(17763, dense=True), ollama)
    assert exc_info.value.code == "ai_timeout"
    assert len(ollama.calls) == 2


def test_non_timeout_error_never_triggers_the_fallback():
    ollama = ScriptedOllama(DocPipeError("ai_unavailable", "x"), EMPTY_RESULT)
    with pytest.raises(DocPipeError) as exc_info:
        _run(build_contract(17763, dense=True), ollama)
    assert exc_info.value.code == "ai_unavailable"
    assert len(ollama.calls) == 1


def test_fallback_disabled_means_one_call_with_full_timeout():
    settings = {"contract_extraction": TextOptimizationSettings(timeout_fallback_enabled=False)}
    ollama = ScriptedOllama(_timeout(), EMPTY_RESULT)
    with pytest.raises(DocPipeError):
        _run(build_contract(17763, dense=True), ollama, settings=settings)
    assert len(ollama.calls) == 1
    assert ollama.calls[0]["timeout_seconds"] == PROFILE.timeout_seconds


def test_no_fallback_when_too_little_time_is_left():
    profile = dataclasses.replace(PROFILE, timeout_seconds=40)  # 35% of 40s < 15s minimum
    ollama = ScriptedOllama(_timeout(), EMPTY_RESULT)
    with pytest.raises(DocPipeError):
        _run(build_contract(17763, dense=True), ollama, profile=profile)
    assert len(ollama.calls) == 1


# --- Runner: unchanged paths -----------------------------------------------


def test_non_optimizable_mode_gets_the_raw_text_unchanged_in_one_call():
    raw = "  Wartungsbericht\n\n\n\nSeite 1 von 1  " + "x" * 30000
    ollama = ScriptedOllama({"x": 1})
    outcome = run_extraction(
        mode=MAINTENANCE,
        prompt_template="{context_block}\n{text}",
        context={},
        text=raw,
        profile=PROFILE,
        ollama_client=ollama,
        optimization_settings=default_text_optimization(),
    )
    assert outcome.optimization is None
    assert ollama.calls[0]["prompt"].endswith(raw)
    assert ollama.calls[0]["timeout_seconds"] == PROFILE.timeout_seconds
    assert "deadline" not in ollama.calls[0]


def test_disabled_optimization_sends_the_raw_contract_text():
    raw = build_contract(17763, pages=6)
    settings = {"contract_extraction": TextOptimizationSettings(enabled=False)}
    ollama = ScriptedOllama(EMPTY_RESULT)
    outcome = _run(raw, ollama, settings=settings)
    assert outcome.optimization is None
    assert _document_text(ollama.calls[0]["prompt"]) == raw


def test_profile_context_window_is_used_for_the_budget_and_sent_as_num_ctx():
    raw = build_contract(17763, pages=6, dense=True)
    small, large = ScriptedOllama(EMPTY_RESULT), ScriptedOllama(EMPTY_RESULT)
    small_stats = _run(raw, small).optimization
    large_stats = _run(
        raw, large, profile=dataclasses.replace(PROFILE, context_window_tokens=8192)
    ).optimization
    assert small_stats["context_window_tokens"] == 4096 and small.calls[0]["num_ctx"] is None
    assert large_stats["context_window_tokens"] == 8192 and large.calls[0]["num_ctx"] == 8192
    assert large_stats["call_budget_chars"] > small_stats["call_budget_chars"]
    assert small_stats["estimated_prompt_tokens"] < 4096


# --- Routes ----------------------------------------------------------------


def _contract_result() -> dict:
    return {
        **EMPTY_RESULT,
        "vendor_name": "Musterlift Aufzugsservice GmbH",
        "amount": 450.0,
        "currency": "EUR",
    }


def test_analyze_response_keeps_contract_and_adds_optional_diagnostics(client):
    fake = client.fake_ollama["client"]
    fake.result = _contract_result()
    raw = build_contract(17763, pages=6, dense=True)
    response = client.post(
        "/api/v1/documents/analyze",
        headers=auth_headers(AI_KEY),
        json={"mode": "contract_extraction", "context": {"document_type": "contract"}, "text": raw},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["result"] == _contract_result()
    assert {"mode", "model_profile", "model", "prompt_version", "result"} <= set(data)
    diag = data["text_optimization"]
    assert diag["strategy"] == "relevance"
    assert diag["raw_chars"] == len(raw)
    assert diag["optimized_chars"] < diag["normalized_chars"] < diag["raw_chars"]
    assert diag["ai_calls"] == 1
    assert {"normalization_ms", "optimization_ms", "ai_ms"} <= set(diag)
    assert all(not isinstance(v, str) or v == "relevance" for v in diag.values())
    assert len(_document_text(fake.last_prompt)) == diag["optimized_chars"]


def test_analyze_other_modes_have_null_diagnostics_and_raw_text(client):
    raw = "Wartungsbericht   KONE\n\n\n\nSeite 1 von 3"
    response = client.post(
        "/api/v1/documents/analyze",
        headers=auth_headers(AI_KEY),
        json={"mode": "maintenance_extraction", "text": raw},
    )
    assert response.status_code == 200
    assert response.json()["data"]["text_optimization"] is None
    assert raw in client.fake_ollama["client"].last_prompt


def test_analyze_log_line_has_sizes_and_strategy_but_no_document_content(client, caplog):
    raw = build_contract(17763, pages=6, dense=True)
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "contract_extraction", "text": raw},
        )
    [record] = [r for r in caplog.records if r.name == "docpipe.ai"]
    line = record.getMessage()
    assert "strategy=relevance" in line
    assert f"input_chars={len(raw)}" in line
    assert "optimized_chars=" in line and "normalization_ms=" in line and "ai_calls=1" in line
    for snippet in ("WV-2026-0815", "Musterlift", "Kündigung", "450,00"):
        assert snippet not in line


def test_analyze_timeout_after_fallback_is_a_normal_ai_timeout_and_logged(client, caplog):
    client.fake_ollama["client"].mode = "timeout"
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        response = client.post(
            "/api/v1/documents/analyze",
            headers=auth_headers(AI_KEY),
            json={"mode": "contract_extraction", "text": build_contract(17763, dense=True)},
        )
    assert response.status_code == 504
    assert response.json()["code"] == "ai_timeout"
    # conftest profiles have a 5s timeout - too short for a fallback, so one call.
    assert client.fake_ollama["client"].calls == 1
    [record] = [r for r in caplog.records if r.name == "docpipe.ai"]
    assert "status=ai_timeout" in record.getMessage() and "strategy=relevance" in record.getMessage()


def test_lab_draft_analyze_uses_the_same_optimization(client):
    response = client.post(
        "/api/v1/lab/analyze",
        headers=auth_headers(LAB_KEY),
        json={
            "mode": "contract_extraction",
            "text": build_contract(17763, dense=True),
            "prompt": "Draft.\n{context_block}\nDOCUMENT TEXT:\n{text}",
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["text_optimization"]["strategy"] == "relevance"


@pytest.fixture
def num_ctx_client(fake_stirling_holder, fake_ollama_holder, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    settings = _build_test_settings(tmp_path / "runtime-prompts")
    models = {
        name: dataclasses.replace(profile, context_window_tokens=8192)
        for name, profile in settings.models.items()
    }
    monkeypatch.setattr(main_module, "load_settings", lambda: dataclasses.replace(settings, models=models))
    with TestClient(main_module.app) as test_client:
        test_client.fake_ollama = fake_ollama_holder
        yield test_client


def test_profile_context_window_reaches_analyze_assistant_and_warmup(num_ctx_client):
    fake = num_ctx_client.fake_ollama["client"]
    num_ctx_client.post(
        "/api/v1/documents/analyze",
        headers=auth_headers(AI_KEY),
        json={"mode": "maintenance_extraction", "text": "Bericht"},
    )
    assert fake.last_num_ctx == 8192
    num_ctx_client.post(
        "/api/v1/documents/warmup", headers=auth_headers(AI_KEY), json={"mode": "contract_extraction"}
    )
    assert fake.last_warmup_num_ctx == 8192


# --- OllamaClient: num_ctx and deadline --------------------------------------


SCHEMA = {"type": "object", "properties": {"foo": {"type": "string"}}, "required": ["foo"]}


def _mock_client(handler) -> OllamaClient:
    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_num_ctx_is_sent_only_when_configured():
    payloads = []

    def handler(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"response": json.dumps({"foo": "x"})})

    client = _mock_client(handler)
    client.generate_structured("p", SCHEMA, model="m", timeout_seconds=5, temperature=0)
    client.generate_structured("p", SCHEMA, model="m", timeout_seconds=5, temperature=0, num_ctx=8192)
    client.warm_up(model="m", timeout_seconds=5, num_ctx=8192)
    client.warm_up(model="m", timeout_seconds=5)
    assert payloads[0]["options"] == {"temperature": 0}
    assert payloads[1]["options"] == {"temperature": 0, "num_ctx": 8192}
    assert payloads[2]["options"] == {"num_ctx": 8192}
    assert "options" not in payloads[3]


def test_deadline_bounds_each_http_timeout_and_expired_deadline_never_calls():
    timeouts = []

    def handler(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"response": json.dumps({"foo": "x"})})

    client = _mock_client(handler)
    client.generate_structured(
        "p", SCHEMA, model="m", timeout_seconds=100, temperature=0, deadline=time.monotonic() + 10
    )
    assert timeouts[0] <= 10
    with pytest.raises(DocPipeError) as exc_info:
        client.generate_structured(
            "p", SCHEMA, model="m", timeout_seconds=100, temperature=0, deadline=time.monotonic() - 1
        )
    assert exc_info.value.code == "ai_timeout"
    assert len(timeouts) == 1


# --- Config ------------------------------------------------------------------


_BASE_CONFIG = """
stirling:
  base_url: "http://stirling:8080"
models:
  light: {{provider: ollama, model: a, timeout_seconds: 60}}
  standard: {{provider: ollama, model: b, timeout_seconds: 150{standard_extra}}}
  heavy: {{provider: ollama, model: c, timeout_seconds: 180}}
{extra}
"""


def _load(tmp_path, monkeypatch, extra: str = "", standard_extra: str = ""):
    path = tmp_path / "config.yaml"
    path.write_text(_BASE_CONFIG.format(extra=extra, standard_extra=standard_extra), encoding="utf-8")
    monkeypatch.setenv("DOCPIPE_CONFIG", str(path))
    return load_settings()


def test_text_optimization_defaults_to_enabled_for_contracts_only(tmp_path, monkeypatch):
    settings = _load(tmp_path, monkeypatch)
    assert set(settings.text_optimization) == {"contract_extraction"}
    assert settings.text_optimization["contract_extraction"] == TextOptimizationSettings()
    assert settings.text_optimization["contract_extraction"].enabled is True
    assert settings.models["standard"].context_window_tokens is None


def test_text_optimization_overrides_and_context_window_are_parsed(tmp_path, monkeypatch):
    extra = (
        "text_optimization:\n  contract_extraction:\n    direct_limit_chars: 9000\n"
        "    chunking_enabled: false\n    primary_timeout_ratio: 0.5\n"
    )
    settings = _load(tmp_path, monkeypatch, extra=extra, standard_extra=", context_window_tokens: 8192")
    opt = settings.text_optimization["contract_extraction"]
    assert opt.direct_limit_chars == 9000 and opt.chunking_enabled is False
    assert opt.primary_timeout_ratio == 0.5 and opt.target_chars == TextOptimizationSettings().target_chars
    assert settings.models["standard"].context_window_tokens == 8192


@pytest.mark.parametrize(
    "extra",
    [
        "text_optimization:\n  maintenance_extraction:\n    enabled: true\n",
        "text_optimization:\n  contract_extraction:\n    target_size: 5\n",
        "text_optimization:\n  contract_extraction:\n    target_chars: 0\n",
        "text_optimization:\n  contract_extraction:\n    primary_timeout_ratio: 1.5\n",
        "text_optimization:\n  contract_extraction:\n    assumed_context_window_tokens: 512\n",
        "text_optimization: [1, 2]\n",
    ],
)
def test_invalid_text_optimization_config_fails_fast(tmp_path, monkeypatch, extra):
    with pytest.raises(ConfigError, match="text_optimization"):
        _load(tmp_path, monkeypatch, extra=extra)


def test_too_small_context_window_fails_fast(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="context_window_tokens"):
        _load(tmp_path, monkeypatch, standard_extra=", context_window_tokens: 1000")
