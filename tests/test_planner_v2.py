"""Planner V2 for contract_extraction: mandatory vs. supporting passages,
one-call-first against the HARD per-call budget, target_chars as a SOFT
limit only, chunking only when the mandatory passages don't fit.

Planner tests are pure functions; the request-path tests use the REAL
OllamaClient with a mocked HTTP transport (no real model - the fake model
"reads" only the DOCUMENT TEXT it was sent). All texts are synthetic.
"""

from __future__ import annotations

import json
import re
import time

import httpx
import pytest

from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.prompting import build_prompt
from system.ai.text_optimization.contract import CONTRACT_GROUPS
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.planner import (
    STRATEGY_CHUNKED,
    STRATEGY_DIRECT,
    STRATEGY_RELEVANCE,
    call_budget_chars,
    plan_input,
)
from system.ai.text_optimization.settings import TextOptimizationSettings
from system.config import ConfigError, ModelProfile
from tests.contract_samples import (
    PACKAGE_DETAIL,
    PDF_RELEVANT_SNIPPETS,
    PRICE_TABLE,
    RELEVANT_SNIPPETS,
    RENEWAL,
    build_contract,
    build_pdf_contract,
    distinct_clauses,
    filler_section,
)
from tests.test_contract_text_optimization_pipeline import _load as _load_config

CONTRACT = MODES["contract_extraction"]
TEMPLATE = (PROMPTS_DIR / "contract_extraction" / f"{CONTRACT.default_prompt_version}.txt").read_text(
    encoding="utf-8"
)
_OVERHEAD = len(build_prompt(TEMPLATE, {"document_type": "contract"}, ""))
LARGE_BUDGET = call_budget_chars(8192, _OVERHEAD)  # staging heavy profile
SMALL_BUDGET = call_budget_chars(4096, _OVERHEAD)
SETTINGS = TextOptimizationSettings()


def _plan(text: str, budget: int = LARGE_BUDGET, settings: TextOptimizationSettings = SETTINGS):
    return plan_input(text, CONTRACT_GROUPS, settings, budget)


def _sent(plan) -> str:
    return "\n".join(call.text for call in plan.calls)


def _many_clauses(copies: int, *, detail: bool = True) -> str:
    """Many repeated package clauses (same patterns, different values). With
    `detail` each clause is a long block (PACKAGE_DETAIL), so even the
    bounded mandatory set (Planner V2.1: best clause + one differing value
    per pattern) exceeds the 4,096-window budget."""
    extra = f" {PACKAGE_DETAIL}" if detail else ""
    parts = [build_contract(4000)]
    for i in range(copies):
        parts.append(
            f"§ {20 + i} Preisanpassung Nr. {i}\nDie Vergütung für Paket {i} beträgt {100 + i},00 € "
            f"quartalsweise.{extra}\n\n§ {60 + i} Sonderkündigung Nr. {i}\nKündigung von Paket {i} "
            f"mit einer Frist von {i + 1} Wochen; die Laufzeit verlängert sich sonst um {i + 1} Monate."
            f"{extra}"
        )
        parts.append(filler_section(100 + i, 1200))
    return "\n\n".join(parts)


def _distinct_contract(count: int) -> str:
    """build_contract plus `count` long clauses that each carry a different
    strong pattern - unique mandatory evidence, separated by filler."""
    parts = [build_contract(4000)]
    for i, clause in enumerate(distinct_clauses(count)):
        parts += [clause, filler_section(200 + i, 1200)]
    return "\n\n".join(parts)


def _first_sentence(clause: str) -> str:
    return clause.split("\n", 1)[1].split(". ", 1)[0]


# --- One-call-first / soft vs. hard budget ---------------------------------------


def test_mandatory_fits_one_call_and_weak_prose_is_not_used_to_fill_up():
    plan = _plan(build_contract(17000, dense=True))  # dense = weak hits in every paragraph
    stats = plan.stats
    assert stats["strategy"] == STRATEGY_RELEVANCE and len(plan.calls) == 1
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in _sent(plan), snippet
    # Supporting context is capped, not filled up to target_chars:
    assert stats["supporting_chars"] <= SETTINGS.supporting_context_chars + 100
    assert stats["optimized_chars"] < SETTINGS.target_chars * 0.75


def test_supporting_context_never_forces_chunking():
    # Many weak hits, little mandatory content: even a tiny soft target or
    # no supporting room at all must not change the strategy.
    raw = build_contract(40000, dense=True)
    for settings in (
        SETTINGS,
        TextOptimizationSettings(target_chars=1500),
        TextOptimizationSettings(supporting_context_chars=0),
    ):
        plan = _plan(raw, settings=settings)
        assert plan.strategy == STRATEGY_RELEVANCE and len(plan.calls) == 1, settings
        for snippet in RELEVANT_SNIPPETS:
            assert snippet in _sent(plan), (settings, snippet)


def test_soft_target_exceeded_but_hard_budget_fits_stays_one_call():
    # Unique evidence (10 different clause types) above target_chars but
    # within the real per-call budget: one call, nothing dropped.
    plan = _plan(_distinct_contract(10))
    stats = plan.stats
    assert stats["mandatory_chars"] > SETTINGS.target_chars  # over the soft target ...
    assert stats["strategy"] == STRATEGY_RELEVANCE and len(plan.calls) == 1  # ... still one call
    assert len(plan.calls[0].text) <= LARGE_BUDGET  # never over the hard budget
    assert stats["dropped_passages"] == 0
    for clause in distinct_clauses(10):
        assert _first_sentence(clause) in _sent(plan), clause
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in _sent(plan), snippet


def test_unique_mandatory_evidence_over_the_hard_budget_is_chunked():
    budget = 6000  # below the ~8k of unique evidence in this sample
    plan = _plan(_distinct_contract(10), budget=budget)
    assert plan.strategy == STRATEGY_CHUNKED
    assert all(len(call.text) <= budget for call in plan.calls)
    sent = _sent(plan)
    for clause in distinct_clauses(10):
        assert _first_sentence(clause) in sent, clause


def test_a_lower_target_chars_no_longer_forces_chunking():
    # Real staging finding: target_chars 5,500 turned a 1-call contract
    # into 3 chunks. The decision now only depends on the hard budget.
    # 15 short clause pairs: the V1 planner was 1 call at 7,000 but 3 chunks
    # at 5,500 (~11.7k chars) - the same paradox as on staging.
    raw = _many_clauses(15, detail=False)
    default, low = _plan(raw), _plan(raw, settings=TextOptimizationSettings(target_chars=5500))
    assert default.strategy == low.strategy == STRATEGY_RELEVANCE
    assert len(low.calls) == 1


def test_mandatory_exceeding_the_hard_budget_is_chunked():
    plan = _plan(_many_clauses(25), budget=SMALL_BUDGET)
    assert plan.strategy == STRATEGY_CHUNKED
    assert 2 <= len(plan.calls) <= len(CONTRACT_GROUPS)
    for call in plan.calls:
        assert len(call.text) <= SMALL_BUDGET
        assert call.fields is not None


def test_hard_budget_is_never_exceeded():
    samples = [build_contract(30000, dense=True), _many_clauses(25), build_pdf_contract(78000, pages=26)]
    for budget in (1500, SMALL_BUDGET, 6000, LARGE_BUDGET):
        for raw in samples:
            for call in _plan(raw, budget=budget).calls:
                assert len(call.text) <= budget, (budget, len(call.text))


def test_short_contract_stays_direct_and_complete():
    raw = build_contract(3000)
    plan = _plan(raw)
    assert plan.strategy == STRATEGY_DIRECT
    assert plan.calls[0].text == normalize_text(raw)
    assert plan.stats["supporting_chars"] == 0
    assert plan.stats["mandatory_chars"] == plan.stats["normalized_chars"]


def test_planning_is_deterministic():
    raw = build_pdf_contract(40000, pages=14)
    first, second = _plan(raw), _plan(raw)
    assert [c.text for c in first.calls] == [c.text for c in second.calls]
    assert first.stats["mandatory_chars"] == second.stats["mandatory_chars"]


# --- Mandatory coverage -------------------------------------------------------------


def test_all_core_groups_reach_the_model_from_a_large_spread_contract():
    plan = _plan(build_pdf_contract(78000, pages=26))
    sent = _sent(plan)
    for snippet in PDF_RELEVANT_SNIPPETS:
        assert snippet in sent, snippet
    assert plan.stats["optimized_chars"] < SETTINGS.target_chars


def test_weak_only_payment_interval_clause_is_mandatory():
    # The interval is stated in a sentence without any strong finance
    # keyword (price/amount) - only weak evidence ("quartalsweise",
    # "Rechnung"). It must not be dropped as mere supporting context.
    # Placed in the middle of the document, away from head and tail.
    interval = "§ 9 Abrechnung\nDie Rechnungsstellung erfolgt quartalsweise im Voraus."
    raw = _in_the_middle(build_contract(30000, dense=True), interval)
    plan = _plan(raw, settings=TextOptimizationSettings(supporting_context_chars=0))
    assert "Die Rechnungsstellung erfolgt quartalsweise im Voraus." in _sent(plan)


def test_contradicting_clauses_are_both_mandatory():
    text = build_contract(20000, dense=True).replace(
        "Kündigungsfrist: 3 Monate zum Vertragsende.",
        "Kündigungsfrist: 3 Monate zum Vertragsende.\n\n"
        + filler_section(77, 3000, dense=True)
        + "\n\n§ 15 Abweichende Regelung\nAbweichend von § 14 beträgt die Kündigungsfrist 6 Monate.",
    )
    sent = _sent(_plan(text, settings=TextOptimizationSettings(supporting_context_chars=0)))
    assert "Kündigungsfrist: 3 Monate" in sent and "Kündigungsfrist 6 Monate" in sent


def _in_the_middle(contract: str, passage: str) -> str:
    """Inserts `passage` after the renewal clause (middle of build_contract),
    surrounded by neutral filler - far from head and tail, so only the
    mandatory tier can bring it in."""
    before, after = filler_section(98, 1500, dense=True), filler_section(97, 1500, dense=True)
    return contract.replace(RENEWAL, f"{RENEWAL}\n\n{before}\n\n{passage}\n\n{after}", 1)


def _filler_lines(prefix: str, count: int) -> str:
    # Complete sentences without contract keywords, one per line (no joins).
    return "\n".join(f"{prefix} {i}: Die Parteien arbeiten vertrauensvoll zusammen." for i in range(count))


def test_digit_initial_continuation_in_the_same_block_stays_together():
    raw = _in_the_middle(
        build_contract(20000, dense=True),
        "§ 21 Sonderregel\nDie Kündigungsfrist beträgt\n3 Monate zum Ende der Vertragslaufzeit.",
    )
    sent = _sent(_plan(raw, settings=TextOptimizationSettings(supporting_context_chars=0)))
    assert "Die Kündigungsfrist beträgt\n3 Monate zum Ende der Vertragslaufzeit." in sent


@pytest.mark.parametrize(
    "notice_at_end, months",
    [
        (False, 3),  # the split clause is the only notice clause
        (True, 6),  # it contradicts § 14 (3 Monate) - its value is only in the second block
    ],
)
def test_digit_initial_continuation_split_across_blocks_is_kept_with_its_hit(notice_at_end, months):
    # A long paragraph is split by segmentation exactly between
    # "... beträgt" and "N Monate ..." - the second half has no keyword of
    # its own and is a large block, but it continues the hit mid-sentence.
    lead = _filler_lines("Absatz A", 14)
    paragraph = (
        f"{lead}\nDie Kündigungsfrist beträgt\n{months} Monate zum Ende eines Kalendervierteljahres.\n"
        f"{_filler_lines('Absatz B', 20)}"
    )
    raw = _in_the_middle(build_contract(20000, dense=True, notice_at_end=notice_at_end), paragraph)
    normalized = normalize_text(raw)
    plan = _plan(raw, settings=TextOptimizationSettings(supporting_context_chars=0))
    # Precondition: the cut really falls between the two parts.
    from system.ai.text_optimization.passages import segment_blocks

    cut_blocks = [normalized[s:e] for s, e in segment_blocks(normalized)]
    assert any(b.endswith("Die Kündigungsfrist beträgt") for b in cut_blocks)
    sent = _sent(plan)
    assert "Die Kündigungsfrist beträgt" in sent
    assert f"{months} Monate zum Ende eines Kalendervierteljahres." in sent


def test_a_passage_relevant_for_several_groups_is_sent_once():
    combined = (
        "§ 30 Laufzeit und Vergütung\nDer Vertrag beginnt am 01.03.2026, die Laufzeit beträgt 24 Monate; "
        "die Vergütung beträgt 980,00 € monatlich."
    )
    raw = build_contract(25000, dense=True) + "\n\n" + combined
    plan = _plan(raw)
    assert plan.strategy == STRATEGY_RELEVANCE
    assert _sent(plan).count("die Vergütung beträgt 980,00 € monatlich") == 1


def test_excerpts_keep_document_order():
    normalized = normalize_text(build_pdf_contract(60000, pages=20))
    plan = _plan(build_pdf_contract(60000, pages=20))
    parts = plan.calls[0].text.split("[Excerpt")[1:]
    bodies = [part.split("]\n", 1)[1].removesuffix("\n\n") for part in parts]
    assert len(bodies) >= 2
    positions = [normalized.index(body) for body in bodies]
    assert positions == sorted(positions)


def test_price_table_stays_whole():
    assert PRICE_TABLE in _sent(_plan(build_contract(30000, dense=True)))


# --- Safe fallback -------------------------------------------------------------------


def test_no_keyword_contract_still_gets_a_representative_sample():
    sections = [f"Abschnitt {i}\n" + ("Lorem ipsum dolor sit amet. " * 40) for i in range(40)]
    plan = _plan("\n\n".join(sections))
    sent = _sent(plan)
    assert plan.strategy == STRATEGY_RELEVANCE
    for marker in ("Abschnitt 0\n", "Abschnitt 20\n", "Abschnitt 39\n"):  # head, middle sample, tail
        assert marker in sent, marker
    assert len(sent) > SETTINGS.target_chars * 0.6  # the fallback may use more context


def test_weak_only_contract_is_not_reduced_to_almost_nothing():
    # No strong keyword at all, only weak ones (EN "term", "payment", ...):
    # each group's best weak block is mandatory, and the text sent is not tiny.
    sections = [
        f"Section {i}\nThe parties agree on the services and the payment of the invoice for the term. "
        + "General provisions apply. " * 30
        for i in range(30)
    ]
    plan = _plan("\n\n".join(sections))
    assert plan.stats["mandatory_chars"] > 1500
    assert plan.stats["optimized_chars"] > 2500


# --- Contract number regression (generic) -------------------------------------------


TITLE_ONLY = (
    "Muster Care Servicevertrag\n\n"
    "zwischen Testhaus Beispiel GmbH (Auftraggeber)\nund Musterlift Testservice GmbH (Auftragnehmer)\n\n"
    "§ 1 Vertragsgegenstand\nWartung der Aufzugsanlagen nach dem Leistungsprogramm Care.\n\n"
    "§ 2 Laufzeit\nDer Vertrag beginnt am 01.01.2026.\n\n"
    "§ 3 Vergütung\nDie Vergütung beträgt 1.200,00 EUR monatlich.\n\n"
)


def test_title_without_contract_number_is_sent_as_is_and_nothing_number_like_is_added():
    raw = TITLE_ONLY + "\n\n".join(filler_section(40 + i, 1400, dense=True) for i in range(20))
    plan = _plan(raw)
    sent = _sent(plan)
    assert plan.strategy == STRATEGY_RELEVANCE and len(plan.calls) == 1
    assert "Muster Care Servicevertrag" in sent  # title reaches the model as the document title
    assert not re.search(r"vertrags?[- ]?(nummer|nr)", sent, re.I)  # no number label invented/added
    # The active prompt keeps the explicit-number-only rule.
    assert "only if explicitly printed in the document" in TEMPLATE
    assert "never construct one" in TEMPLATE


def _real_client(answer) -> tuple:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        body = answer(payload, len(requests))
        if isinstance(body, Exception):
            raise body
        return httpx.Response(200, json=body)

    from system.services.ollama import OllamaClient

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, requests


def _empty() -> dict:
    return {field: None for field in CONTRACT.response_schema["properties"]}


def _run(text, client, profile):
    return run_extraction(
        mode=CONTRACT,
        prompt_template=TEMPLATE,
        context={},
        text=text,
        profile=profile,
        ollama_client=client,
        optimization_settings={"contract_extraction": SETTINGS},
    )


PROFILE_8K = ModelProfile(
    provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=8192
)
PROFILE_4K = ModelProfile(provider="ollama", model="m", timeout_seconds=60, temperature=0)


def _document(payload) -> str:
    return payload["prompt"].split("DOCUMENT TEXT:\n", 1)[1]


def test_title_only_contract_keeps_contract_number_null_through_the_real_client():
    def answer(payload, n):
        result = _empty()
        result["contract_type"] = "Muster Care Servicevertrag"
        if m := re.search(r"Vertragsnummer:\s*(\S+)", _document(payload)):
            result["contract_number"] = m.group(1)
        return {"response": json.dumps(result), "done": True, "prompt_eval_count": 900, "eval_count": 80}

    raw = TITLE_ONLY + "\n\n".join(filler_section(40 + i, 1400, dense=True) for i in range(20))
    client, requests = _real_client(answer)
    outcome = _run(raw, client, PROFILE_8K)
    assert outcome.result["contract_number"] is None
    assert outcome.result["contract_type"] == "Muster Care Servicevertrag"
    assert len(requests) == 1 and outcome.optimization["prompt_eval_count_total"] == 900


def test_a_non_basis_chunk_guessing_the_title_as_contract_number_is_ignored():
    # Chunked: only the basis chunk owns contract_number; a term/finance
    # chunk that "sees" the title and fills it in must not win the merge.
    def answer(payload, n):
        result = _empty()
        if n > 1:  # non-basis chunks guess
            result["contract_number"] = "Muster Care Servicevertrag"
        return {"response": json.dumps(result), "done": True}

    raw = TITLE_ONLY + _many_clauses(25)
    client, requests = _real_client(answer)
    outcome = _run(raw, client, PROFILE_4K)
    assert outcome.optimization["strategy"] == STRATEGY_CHUNKED and len(requests) >= 2
    assert outcome.result["contract_number"] is None


# --- Request path: real OllamaClient, mocked transport ----------------------------------


def test_relevance_one_call_with_repair_and_token_telemetry():
    def answer(payload, n):
        if n == 1:
            return {"response": "not json", "done": True, "prompt_eval_count": 1200, "eval_count": 7}
        return {"response": json.dumps(_empty()), "done": True, "prompt_eval_count": 1500, "eval_count": 90}

    client, requests = _real_client(answer)
    outcome = _run(_many_clauses(25), client, PROFILE_8K)
    stats = outcome.optimization
    assert stats["strategy"] == STRATEGY_RELEVANCE and stats["ai_calls"] == 1 and len(requests) == 2
    assert stats["calls"][0]["repair_used"] is True
    assert (stats["prompt_eval_count_total"], stats["eval_count_total"]) == (2700, 97)


def test_chunked_calls_merge_and_report_per_call_tokens():
    def answer(payload, n):
        result = _empty()
        if "Preisanpassung" in _document(payload) and "Kündigungsfrist: 3 Monate" not in _document(payload):
            result.update(amount=100.0, currency="EUR", payment_interval="quarterly")
        return {"response": json.dumps(result), "done": True, "prompt_eval_count": 1000 * n, "eval_count": 10}

    client, requests = _real_client(answer)
    outcome = _run(_many_clauses(25), client, PROFILE_4K)
    stats = outcome.optimization
    assert stats["strategy"] == STRATEGY_CHUNKED
    assert len(stats["calls"]) == len(requests) == stats["chunk_count"]
    assert stats["prompt_eval_count_total"] == sum(1000 * (i + 1) for i in range(len(requests)))
    assert [c["label"] for c in stats["calls"]][0] == "basis"


def test_timeout_then_reduced_fallback_on_the_real_client():
    def answer(payload, n):
        if n == 1:
            return httpx.ReadTimeout("slow")
        return {"response": json.dumps(_empty()), "done": True, "prompt_eval_count": 800, "eval_count": 60}

    client, requests = _real_client(answer)
    outcome = _run(_many_clauses(25), client, PROFILE_8K)
    stats = outcome.optimization
    assert stats["fallback_used"] is True and len(requests) == 2
    assert len(_document(requests[1])) < len(_document(requests[0]))
    assert stats["prompt_eval_count_total"] is None  # timed-out call never reported


# --- Config + performance -------------------------------------------------------------


@pytest.mark.parametrize("value, ok", [(0, True), (2500, True), (-1, False), ("viel", False)])
def test_supporting_context_chars_is_configurable_and_validated(tmp_path, monkeypatch, value, ok):
    extra = f"text_optimization:\n  contract_extraction:\n    supporting_context_chars: {value}\n"
    if ok:
        settings = _load_config(tmp_path, monkeypatch, extra=extra)
        assert settings.text_optimization["contract_extraction"].supporting_context_chars == value
    else:
        with pytest.raises(ConfigError):
            _load_config(tmp_path, monkeypatch, extra=extra)


def test_supporting_context_default():
    assert TextOptimizationSettings().supporting_context_chars == 1500


def test_planner_v2_stays_fast_and_linear():
    raw_100k, raw_25k = build_pdf_contract(100_000, pages=33), build_pdf_contract(25_000, pages=9)
    started = time.perf_counter()
    _plan(raw_100k)
    elapsed_100k = time.perf_counter() - started
    started = time.perf_counter()
    _plan(raw_25k)
    elapsed_25k = time.perf_counter() - started
    assert elapsed_100k < 2.0
    assert elapsed_100k < max(elapsed_25k, 0.01) * 10
