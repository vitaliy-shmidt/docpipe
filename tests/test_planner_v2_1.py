"""Planner V2.1 - mandatory coverage bounding (contract_extraction).

Regression for the real staging finding: on a ~78k service contract the
V2 planner made every strong hit mandatory (generic "Leistungsumfang",
"Preis", "Kündigung", "Laufzeit" repeated per installation), chunked into
3 calls of ~14k chars each (40,629 chars, 17,083 prompt tokens, timeouts).

V2.1 keeps enough evidence to cover every field group - the best clause
per strong pattern plus one clause with a differing value (contradiction
signal) - and demotes redundant repeats to supporting context. All texts
are synthetic; Ollama is mocked (real OllamaClient, mocked transport).
"""

from __future__ import annotations

import json
import time

import httpx

from system.ai.extraction_runner import run_extraction
from system.ai.text_optimization.contract import CONTRACT_GROUPS
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.passages import STRONG_WEIGHT, score_blocks, segment_blocks
from system.ai.text_optimization.planner import STRATEGY_CHUNKED, STRATEGY_RELEVANCE, plan_input
from system.ai.text_optimization.settings import TextOptimizationSettings
from system.config import ModelProfile
from system.services.ollama import OllamaClient
from tests.contract_samples import (
    HEAD,
    SERVICE_CORE_SNIPPETS,
    build_service_contract,
    filler_section,
)
from tests.test_planner_v2 import CONTRACT, LARGE_BUDGET, SMALL_BUDGET, TEMPLATE

NO_SUPPORT = TextOptimizationSettings(supporting_context_chars=0, tail_chars=0)


def _plan(text, budget=LARGE_BUDGET, settings=TextOptimizationSettings()):
    return plan_input(text, CONTRACT_GROUPS, settings, budget)


def _sent(plan) -> str:
    return "\n".join(call.text for call in plan.calls)


def _significant_chars(text: str) -> int:
    normalized = normalize_text(text)
    blocks = segment_blocks(normalized)
    scores = score_blocks(normalized, blocks, CONTRACT_GROUPS)
    return sum(e - s for (s, e), score in zip(blocks, scores) if max(score.values()) >= STRONG_WEIGHT)


def _spread(core: list[str], repeats: list[str]) -> str:
    """Core clauses at the start, look-alike repeats spread over the rest,
    separated by neutral filler."""
    parts = [HEAD, *core]
    for i, clause in enumerate(repeats):
        parts += [filler_section(300 + i, 900), clause]
    return "\n\n".join(parts)


# --- The staging regression, synthetically ---------------------------------------


def test_kone_like_service_contract_keeps_mandatory_bounded_in_one_call():
    raw = build_service_contract(100)  # ~70k chars, ~100 installation sections
    assert 50_000 < len(raw) < 80_000
    plan = _plan(raw)
    stats = plan.stats
    # V2 made all of these mandatory: tens of thousands of chars, more than
    # one call holds -> 3 chunks of ~14k each.
    assert _significant_chars(raw) > 30_000
    assert stats["mandatory_candidate_count"] >= 100
    # V2.1: bounded to the evidence the fields need.
    assert stats["strategy"] == STRATEGY_RELEVANCE and len(plan.calls) == 1
    assert stats["mandatory_block_count"] <= 25
    assert stats["mandatory_chars"] < 6_000
    assert stats["optimized_chars"] < 9_000
    for snippet in SERVICE_CORE_SNIPPETS:
        assert snippet in _sent(plan), snippet


def test_redundant_hits_do_not_force_chunking_even_in_a_small_window():
    plan = _plan(build_service_contract(100), budget=SMALL_BUDGET)
    assert plan.strategy == STRATEGY_RELEVANCE
    for snippet in SERVICE_CORE_SNIPPETS:
        assert snippet in _sent(plan), snippet


def test_fifty_repeated_strong_hits_of_one_group_are_not_all_mandatory():
    repeats = [
        f"§ {100 + i} Anlage {i}\nDer Leistungsumfang für Anlage {i} entspricht Anlage A." for i in range(60)
    ]
    plan = _plan(_spread([], repeats), settings=NO_SUPPORT)
    stats = plan.stats
    assert stats["mandatory_candidate_count"] >= 60
    assert stats["mandatory_block_count"] < 15
    assert "Leistungsumfang" in _sent(plan)  # the evidence itself stays


# --- Coverage and contradictions ------------------------------------------------------


CORE = [
    "§ 1 Vertragsgegenstand\nGegenstand ist die Wartung der Fahrtreppen.",
    "§ 2 Vertragsbeginn\nDer Vertragsbeginn ist der 01.07.2026.",
    "§ 3 Kündigung\nDie Kündigungsfrist beträgt 3 Monate zum Jahresende.",
    "§ 4 Verlängerung\nDer Vertrag verlängert sich automatisch um 12 Monate.",
    "§ 5 Vergütung\nDie Vergütung beträgt 250 EUR monatlich.",
]


def test_one_clause_per_group_is_all_mandatory():
    repeats = [
        f"§ {100 + i} Anlage {i}\nDer Leistungsumfang für Anlage {i} ist im Preis enthalten."
        for i in range(40)
    ]
    sent = _sent(_plan(_spread(CORE, repeats), settings=NO_SUPPORT))
    for clause in CORE:
        assert clause.split("\n", 1)[1] in sent, clause


def test_same_value_repeats_are_sent_once_as_mandatory():
    repeats = [f"§ {100 + i} Hinweis\nDie Vergütung beträgt 250 EUR monatlich." for i in range(20)]
    sent = _sent(_plan(_spread(CORE, repeats), settings=NO_SUPPORT))
    assert sent.count("Die Vergütung beträgt 250 EUR monatlich.") == 1


def test_contradicting_notice_periods_both_stay_despite_bounding():
    repeats = [
        f"§ {100 + i} Anlage {i}\nEine Kündigung für Anlage {i} richtet sich nach § 3." for i in range(40)
    ]
    repeats[30] = "§ 130 Abweichende Regelung\nAbweichend von § 3 beträgt die Kündigungsfrist 6 Monate."
    sent = _sent(_plan(_spread(CORE, repeats), settings=NO_SUPPORT))
    assert "Kündigungsfrist beträgt 3 Monate" in sent
    assert "Kündigungsfrist 6 Monate" in sent


def test_different_prices_both_stay_despite_bounding():
    repeats = [
        f"§ {100 + i} Anlage {i}\nDie Vergütung für Anlage {i} ist im Gesamtpreis enthalten."
        for i in range(40)
    ]
    # Neutral heading: only the differing value (300 vs. 250 EUR) keeps it.
    repeats[25] = "§ 125 Zweites Jahr\nAb dem zweiten Vertragsjahr beträgt die Vergütung 300 EUR monatlich."
    sent = _sent(_plan(_spread(CORE, repeats), settings=NO_SUPPORT))
    assert "250 EUR monatlich" in sent and "300 EUR monatlich" in sent


def test_table_header_and_value_rows_across_a_blank_line_stay_together():
    # The value row repeats an already known amount, so it is no new
    # evidence on its own - only the table link to its header row keeps it.
    table = "§ 7 Preisübersicht\nLeistung      Preis      Intervall\n\nWartung      250 EUR      monatlich"
    repeats = [
        f"§ {100 + i} Anlage {i}\nDer Preis für Anlage {i} ist in der Preisübersicht enthalten."
        for i in range(40)
    ]
    # Filler first, so the table is outside the always-kept document head.
    core = ["§ 5 Vergütung\nDie Vergütung beträgt 250 EUR monatlich.", filler_section(299, 1600), table]
    sent = _sent(_plan(_spread(core, repeats), settings=NO_SUPPORT))
    assert "Leistung  Preis  Intervall" in sent
    assert "Wartung  250 EUR  monatlich" in sent


def test_the_main_clause_wins_over_many_look_alike_special_clauses():
    # 25 package clauses each mention notice/renewal and score higher than
    # the contract's own "§ 5 Verlängerung" / "§ 14 Kündigung" clauses. The
    # clause whose heading names the topic is the evidence that must stay.
    from tests.test_planner_v2 import _many_clauses

    sent = _sent(_plan(_many_clauses(25, detail=False), settings=NO_SUPPORT))
    assert "verlängert sich automatisch um jeweils weitere 12 Monate" in sent
    assert "Kündigungsfrist: 3 Monate zum Vertragsende" in sent


def test_provider_without_keyword_in_the_head_is_kept():
    plan = _plan(build_service_contract(100), settings=NO_SUPPORT)
    assert "Musterlift Service GmbH" in _sent(plan)
    assert _sent(plan).startswith("[Excerpt 1/")


def test_neighbor_context_never_grows_into_a_chain():
    # One sentence-less wall of text: every block "continues" into the next.
    # Only the hit's direct neighbors may come along - never the whole wall.
    words = ["wort"] * 6000
    words[3000:3000] = "die Kündigungsfrist beträgt drei Monate".split()
    raw = "WARTUNGSVERTRAG\n\n" + " ".join(words)
    plan = _plan(raw, settings=NO_SUPPORT)
    assert "Kündigungsfrist beträgt drei Monate" in _sent(plan)
    assert plan.stats["mandatory_chars"] < 6 * 1400
    assert plan.stats["optimized_chars"] < len(normalize_text(raw)) / 3


def test_bounded_planning_is_deterministic_and_fast():
    raw = build_service_contract(140)  # ~100k chars
    started = time.perf_counter()
    first = _plan(raw)
    elapsed = time.perf_counter() - started
    assert elapsed < 2.0
    assert [c.text for c in first.calls] == [c.text for c in _plan(raw).calls]


def test_diagnostics_carry_bounding_counts_without_content():
    stats = _plan(build_service_contract(60)).stats
    for key in ("mandatory_chars", "supporting_chars", "mandatory_candidate_count", "mandatory_block_count"):
        assert isinstance(stats[key], int), key
    assert stats["mandatory_candidate_count"] > stats["mandatory_block_count"] > 0


# --- Real client, mocked transport ----------------------------------------------------


def _client(answer):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        return httpx.Response(200, json=answer(payload, len(requests)))

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, requests


def test_service_contract_runs_as_one_call_with_telemetry_on_the_real_client():
    def answer(payload, n):
        result = {field: None for field in CONTRACT.response_schema["properties"]}
        document = payload["prompt"].split("DOCUMENT TEXT:\n", 1)[1]
        if "Kündigungsfrist beträgt 6 Monate" in document:
            result.update(notice_period_value=6, notice_period_unit="months")
        if "2.480,00 EUR netto monatlich" in document:
            result.update(amount=2480.0, currency="EUR", payment_interval="monthly")
        return {"response": json.dumps(result), "done": True, "prompt_eval_count": 2100, "eval_count": 120}

    client, requests = _client(answer)
    profile = ModelProfile(
        provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=8192
    )
    outcome = run_extraction(
        mode=CONTRACT,
        prompt_template=TEMPLATE,
        context={},
        text=build_service_contract(100),
        profile=profile,
        ollama_client=client,
        optimization_settings={"contract_extraction": TextOptimizationSettings()},
    )
    stats = outcome.optimization
    assert len(requests) == 1 and stats["strategy"] == STRATEGY_RELEVANCE
    assert outcome.result["notice_period_value"] == 6 and outcome.result["amount"] == 2480.0
    assert stats["prompt_eval_count_total"] == 2100 and stats["calls"][0]["text_chars"] < 9_000


def test_genuinely_large_unique_evidence_still_chunks():
    # Many different clause types (not repeats) beyond the budget: chunked,
    # never truncated into one call.
    from tests.test_planner_v2 import _distinct_contract

    plan = _plan(_distinct_contract(16), budget=6000)
    assert plan.strategy == STRATEGY_CHUNKED
    assert all(len(call.text) <= 6000 for call in plan.calls)
