"""Contract extraction prompt v2 + Ollama token telemetry.

- Prompt v2 = v1 plus three clarifications (excerpt markers, table column
  gaps, contradictions across excerpts) - everything else byte-identical;
- OllamaClient reads prompt_eval_count/eval_count defensively (missing,
  partial, zero, invalid types);
- the extraction runner reports exact run totals or None, plus per-call
  values, for direct/relevance/chunked/repair/fallback runs;
- the analyze route exposes them (diagnostics + log line, no content).

Ollama is always mocked - these are tests, not token measurements.
"""

from __future__ import annotations

import dataclasses
import json
import logging

import httpx
import pytest

from system.ai.extraction_runner import call_token_counts, run_extraction
from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.text_optimization.settings import default_text_optimization
from system.config import ModelProfile
from system.errors import DocPipeError
from system.services.ollama import OllamaClient
from tests.conftest import AI_KEY, auth_headers
from tests.contract_samples import build_contract
from tests.test_contract_text_optimization_pipeline import _many_clauses_contract

MODE = "contract_extraction"
CONTRACT = MODES[MODE]
MAINTENANCE = MODES["maintenance_extraction"]
V1 = (PROMPTS_DIR / MODE / "v1.txt").read_text(encoding="utf-8")
V2 = (PROMPTS_DIR / MODE / "v2.txt").read_text(encoding="utf-8")
PROFILE = ModelProfile(provider="ollama", model="m", timeout_seconds=100, temperature=0)
PROFILE_8K = dataclasses.replace(PROFILE, context_window_tokens=8192)
EMPTY_RESULT = {field: None for field in CONTRACT.response_schema["properties"]}


# --- Prompt v2 -----------------------------------------------------------------


def test_v2_stays_shipped_as_rollback_and_v1_unchanged():
    # Default moved on to v3 (tests/test_contract_prompt_v3.py); v2 remains
    # the one-activation rollback, v1 the untouched original.
    assert CONTRACT.default_prompt_version == "v3"
    assert (PROMPTS_DIR / MODE / "v2.txt").is_file()
    assert "[Excerpt" not in V1


def test_v2_explains_excerpt_markers_as_parts_of_one_contract():
    assert "[Excerpt n/m]" in V2
    for phrase in (
        "all excerpts are parts of the SAME contract document",
        "in document order",
        "not separate contracts",
        "spread over several excerpts",
    ):
        assert phrase in V2, phrase


def test_v2_explains_table_columns_without_claiming_every_gap_is_a_table():
    assert 'Multiple spaces or "|" may separate the columns of a table.' in V2
    lowered = V2.lower()
    assert "always a table" not in lowered and "are table columns" not in lowered


def test_v2_keeps_the_contradiction_rule_and_extends_it_across_excerpts():
    assert "also across different excerpts, do not pick one or reconcile them" in V2
    assert "set the affected structured field(s) to null and describe the contradiction" in V2


def test_v2_differs_from_v1_only_by_the_clarifications():
    v1_lines, v2_lines = V1.splitlines(), V2.splitlines()
    assert len(v1_lines) == len(v2_lines)
    changed = [(a, b) for a, b in zip(v1_lines, v2_lines) if a != b]
    assert len(changed) == 2
    for old, new in changed:
        # Additions only: the v1 wording stays (the contradiction line only
        # gains "also across ..." and "or reconcile them").
        assert new.startswith(old.split("do not pick")[0])
    assert changed[0][1].startswith(changed[0][0])


def test_v2_field_list_matches_the_unchanged_schema():
    for name in CONTRACT.response_schema["properties"]:
        assert f"- {name}:" in V2, name
    assert V2.count("{context_block}") == 1 and V2.count("{text}") == 1


def test_v2_core_rules_are_all_still_present():
    for phrase in (
        "with EXACTLY these keys, no more and no fewer",
        "A null is always better than a guess.",
        "Never invent a contract term",
        "Never compute a date from another date plus a duration.",
        "Use context ONLY to resolve",
        "Output ONLY the JSON object described above.",
        "Never output any identifier, database ID",
    ):
        assert phrase in V2, phrase


def test_final_prompt_for_a_relevance_selection_carries_markers_and_their_explanation():
    ollama = TokenOllama(EMPTY_RESULT)
    outcome = _run(build_contract(18000, dense=True), ollama, profile=PROFILE_8K)
    assert outcome.optimization["strategy"] == "relevance"
    [prompt] = [call["prompt"] for call in ollama.calls]
    assert "[Excerpt 1/" in prompt.split("DOCUMENT TEXT:", 1)[1]
    assert "all excerpts are parts of the SAME contract document" in prompt


# --- OllamaClient: reading the counts ----------------------------------------------

_KWARGS = {"model": "m", "timeout_seconds": 5, "temperature": 0}
_SCHEMA = {"type": "object", "properties": {"foo": {"type": "string"}}, "required": ["foo"]}


def _client_returning(*bodies: dict) -> tuple[OllamaClient, list]:
    queue = list(bodies)
    requests: list = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=queue.pop(0))

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, requests


def _ok(**metrics) -> dict:
    return {"response": json.dumps({"foo": "bar"}), "done": True, **metrics}


def _generate_timing(*bodies: dict) -> dict:
    client, _ = _client_returning(*bodies)
    timing: dict = {}
    assert client.generate_structured("p", _SCHEMA, timing=timing, **_KWARGS) == {"foo": "bar"}
    return timing


def test_client_reads_both_token_counts():
    timing = _generate_timing(_ok(prompt_eval_count=1234, eval_count=321))
    assert timing["ollama_primary_prompt_eval_count"] == 1234
    assert timing["ollama_primary_eval_count"] == 321
    assert call_token_counts(timing) == {"prompt_eval_count": 1234, "eval_count": 321}


def test_client_missing_counts_do_not_fail_and_stay_unknown():
    timing = _generate_timing(_ok())
    assert "ollama_primary_prompt_eval_count" not in timing
    assert call_token_counts(timing) == {"prompt_eval_count": None, "eval_count": None}


@pytest.mark.parametrize(
    "metrics, expected",
    [
        ({"prompt_eval_count": 900}, {"prompt_eval_count": 900, "eval_count": None}),
        ({"eval_count": 77}, {"prompt_eval_count": None, "eval_count": 77}),
    ],
)
def test_client_partial_counts(metrics, expected):
    assert call_token_counts(_generate_timing(_ok(**metrics))) == expected


def test_client_real_zero_is_not_unknown():
    timing = _generate_timing(_ok(prompt_eval_count=0, eval_count=0))
    assert call_token_counts(timing) == {"prompt_eval_count": 0, "eval_count": 0}


@pytest.mark.parametrize("bad", ["1234", 12.5, True, -1, None, [1], {"n": 1}])
def test_client_invalid_count_types_are_ignored_without_failing(bad):
    timing = _generate_timing(_ok(prompt_eval_count=bad, eval_count=bad))
    assert call_token_counts(timing) == {"prompt_eval_count": None, "eval_count": None}


def test_client_repair_attempt_counts_are_recorded_and_summed():
    invalid = {"response": "not json", "prompt_eval_count": 1000, "eval_count": 5}
    timing = _generate_timing(invalid, _ok(prompt_eval_count=1300, eval_count=40))
    assert timing["ollama_repair_prompt_eval_count"] == 1300
    assert call_token_counts(timing) == {"prompt_eval_count": 2300, "eval_count": 45}


def test_client_repair_without_counts_makes_the_call_total_unknown():
    invalid = {"response": "not json", "prompt_eval_count": 1000, "eval_count": 5}
    counts = call_token_counts(_generate_timing(invalid, _ok()))
    assert counts == {"prompt_eval_count": None, "eval_count": None}


def test_reading_counts_needs_no_extra_request():
    client, requests = _client_returning(_ok(prompt_eval_count=10, eval_count=2))
    client.generate_structured("p", _SCHEMA, timing={}, **_KWARGS)
    assert len(requests) == 1


def test_warm_up_timing_also_carries_counts_when_reported():
    client, _ = _client_returning({"done": True, "prompt_eval_count": 1, "eval_count": 0})
    timing: dict = {}
    client.warm_up(model="m", timeout_seconds=5, timing=timing)
    assert timing["ollama_prompt_eval_count"] == 1 and timing["ollama_eval_count"] == 0


# --- Runner: aggregation ----------------------------------------------------------


class TokenOllama:
    """generate_structured() double that reports scripted token counts per
    call. Script entries: dict result, DocPipeError, or (result, counts)
    where counts = {"prompt_eval_count": ..., "eval_count": ...,
    "repair": {...}} (missing keys = not reported)."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []

    def generate_structured(self, prompt, schema, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        step = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(step, Exception):
            raise step
        result, counts = step if isinstance(step, tuple) else (step, {})
        timing = kwargs["timing"]
        timing["ollama_primary_duration_ms"] = 10.0
        for field in ("prompt_eval_count", "eval_count"):
            if field in counts:
                timing[f"ollama_primary_{field}"] = counts[field]
        if "repair" in counts:
            timing["ollama_repair_duration_ms"] = 5.0
            for field, value in counts["repair"].items():
                timing[f"ollama_repair_{field}"] = value
        timing["ollama_duration_ms"] = 10.0
        return dict(result)


def _run(text, ollama, *, mode=CONTRACT, profile=PROFILE, template=V2, observed=None):
    return run_extraction(
        mode=mode,
        prompt_template=template,
        context={"document_type": "contract"},
        text=text,
        profile=profile,
        ollama_client=ollama,
        optimization_settings=default_text_optimization(),
        observed=observed,
    )


def _counts(prompt_eval_count=None, eval_count=None, **extra) -> dict:
    counts = {**extra}
    if prompt_eval_count is not None:
        counts["prompt_eval_count"] = prompt_eval_count
    if eval_count is not None:
        counts["eval_count"] = eval_count
    return counts


def _chunked(*counts: dict):
    ollama = TokenOllama(*[(EMPTY_RESULT, c) for c in counts])
    outcome = _run(_many_clauses_contract(), ollama)
    assert outcome.optimization["strategy"] == "chunked"
    assert len(ollama.calls) == len(counts), "test script must match the planned chunk count"
    return outcome.optimization


def _chunk_count() -> int:
    ollama = TokenOllama(EMPTY_RESULT)
    return _run(_many_clauses_contract(), ollama).optimization["chunk_count"]


def test_single_call_totals_equal_the_call():
    ollama = TokenOllama((EMPTY_RESULT, _counts(2417, 368)))
    stats = _run(build_contract(3000), ollama, profile=PROFILE_8K).optimization
    assert stats["strategy"] == "direct"
    assert (stats["prompt_eval_count_total"], stats["eval_count_total"]) == (2417, 368)
    assert stats["calls"] == [
        {"label": "document", "text_chars": stats["optimized_chars"], "prompt_eval_count": 2417,
         "eval_count": 368, "repair_used": False}
    ]  # fmt: skip
    # Character metrics stay alongside.
    assert {"raw_chars", "normalized_chars", "optimized_chars"} <= set(stats)


def test_chunked_totals_are_summed_over_all_calls():
    n = _chunk_count()
    assert n >= 2
    counts = [_counts(1000 + 500 * i, 200 + 50 * i) for i in range(n)]
    stats = _chunked(*counts)
    assert stats["prompt_eval_count_total"] == sum(1000 + 500 * i for i in range(n))
    assert stats["eval_count_total"] == sum(200 + 50 * i for i in range(n))
    assert [c["label"] for c in stats["calls"]][0] == "basis"
    assert [c["prompt_eval_count"] for c in stats["calls"]] == [1000 + 500 * i for i in range(n)]


def test_call_order_does_not_change_the_totals():
    n = _chunk_count()
    counts = [_counts(100 * (i + 1), 10 * (i + 1)) for i in range(n)]
    forward, backward = _chunked(*counts), _chunked(*reversed(counts))
    assert forward["prompt_eval_count_total"] == backward["prompt_eval_count_total"]
    assert forward["eval_count_total"] == backward["eval_count_total"]


def test_one_call_without_counts_makes_the_total_unknown_but_keeps_known_calls():
    n = _chunk_count()
    stats = _chunked(_counts(), *[_counts(1500, 250)] * (n - 1))
    assert stats["prompt_eval_count_total"] is None and stats["eval_count_total"] is None
    assert stats["calls"][0]["prompt_eval_count"] is None
    assert all(c["prompt_eval_count"] == 1500 for c in stats["calls"][1:])


def test_no_call_with_counts_gives_unknown_totals():
    stats = _chunked(*[_counts()] * _chunk_count())
    assert stats["prompt_eval_count_total"] is None and stats["eval_count_total"] is None


def test_real_zero_counts_are_summed_as_zero():
    stats = _chunked(*[_counts(0, 0)] * _chunk_count())
    assert stats["prompt_eval_count_total"] == 0 and stats["eval_count_total"] == 0


def test_partial_fields_are_aggregated_independently():
    stats = _chunked(*[_counts(prompt_eval_count=700)] * _chunk_count())
    assert stats["prompt_eval_count_total"] == 700 * _chunk_count()
    assert stats["eval_count_total"] is None


def test_repair_counts_belong_to_their_call():
    repair = {"prompt_eval_count": 1400, "eval_count": 300}
    ollama = TokenOllama((EMPTY_RESULT, _counts(1000, 9, repair=repair)))
    stats = _run(build_contract(3000), ollama, profile=PROFILE_8K).optimization
    assert stats["prompt_eval_count_total"] == 2400 and stats["eval_count_total"] == 309
    assert stats["calls"][0]["repair_used"] is True
    assert stats["ai_calls"] == 1  # planned calls, unchanged meaning


def test_timeout_fallback_keeps_the_fallback_counts_and_an_unknown_total():
    timeout = DocPipeError("ai_timeout", "AI analysis timed out.")
    ollama = TokenOllama(timeout, (EMPTY_RESULT, _counts(1800, 300)))
    outcome = _run(build_contract(18000, dense=True), ollama, profile=PROFILE_8K)
    stats = outcome.optimization
    assert stats["fallback_used"] is True
    assert [c["prompt_eval_count"] for c in stats["calls"]] == [None, 1800]
    assert stats["prompt_eval_count_total"] is None  # the timed-out call's tokens were never reported


def test_failed_request_still_exposes_token_stats_to_the_logger():
    observed: dict = {}
    ollama = TokenOllama(DocPipeError("ai_unavailable", "down"))
    with pytest.raises(DocPipeError):
        _run(build_contract(3000), ollama, observed=observed)
    assert observed["optimization"]["prompt_eval_count_total"] is None
    assert observed["optimization"]["calls"][0]["prompt_eval_count"] is None


def test_token_counts_never_change_the_extraction_result():
    result = {**EMPTY_RESULT, "vendor_name": "Musterlift GmbH"}
    with_counts = _run(build_contract(3000), TokenOllama((result, _counts(5, 5)))).result
    without = _run(build_contract(3000), TokenOllama(result)).result
    assert with_counts == without == result


# --- Analyze route: diagnostics + log -------------------------------------------------


def _analyze(client, mode=MODE, text="Wartungsvertrag Nr. 4711. Vergütung 450,00 € monatlich."):
    return client.post(
        "/api/v1/documents/analyze",
        json={"mode": mode, "text": text, "context": {}},
        headers=auth_headers(AI_KEY),
    )


def _log_message(caplog) -> str:
    [record] = [r for r in caplog.records if r.name == "docpipe.ai"]
    return record.getMessage()


def test_analyze_exposes_token_totals_in_diagnostics_and_log(client, caplog):
    fake = client.fake_ollama["client"]
    fake.result = dict(EMPTY_RESULT)
    fake.fake_prompt_eval_count, fake.fake_eval_count = 2417, 368
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        response = _analyze(client, text="SECRET-CONTRACT-TEXT Vergütung 450,00 €")
    diagnostics = response.json()["data"]["text_optimization"]
    assert diagnostics["prompt_eval_count_total"] == 2417 and diagnostics["eval_count_total"] == 368
    assert diagnostics["calls"][0]["prompt_eval_count"] == 2417
    message = _log_message(caplog)
    assert "prompt_eval_count=2417 eval_count=368" in message
    assert "SECRET-CONTRACT-TEXT" not in message


def test_analyze_without_reported_counts_succeeds_and_logs_none(client, caplog):
    client.fake_ollama["client"].result = dict(EMPTY_RESULT)
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        response = _analyze(client)
    assert response.status_code == 200
    assert response.json()["data"]["text_optimization"]["prompt_eval_count_total"] is None
    assert "prompt_eval_count=None eval_count=None" in _log_message(caplog)


def test_analyze_non_optimized_mode_logs_the_single_call_counts(client, caplog):
    fake = client.fake_ollama["client"]
    fake.fake_prompt_eval_count, fake.fake_eval_count = 0, 120
    with caplog.at_level(logging.INFO, logger="docpipe.ai"):
        response = _analyze(client, mode="maintenance_extraction", text="Wartungsbericht Aufzug 1.")
    assert response.status_code == 200
    assert response.json()["data"]["text_optimization"] is None
    assert "prompt_eval_count=0 eval_count=120" in _log_message(caplog)
