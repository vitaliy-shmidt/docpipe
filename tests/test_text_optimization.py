"""Contract text optimizer - normalization, passage selection, planning,
merge, budget and performance. Pure functions only, no HTTP, no Ollama.
All texts are synthetic (tests/contract_samples.py)."""

from __future__ import annotations

import time

from system.ai.modes import MODES
from system.ai.prompting import build_prompt
from system.ai.text_optimization.contract import CONTRACT_DEPENDENT_FIELDS, CONTRACT_GROUPS
from system.ai.text_optimization.merge import merge_results
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.passages import score_blocks, segment_blocks, select
from system.ai.text_optimization.planner import (
    MIN_CALL_BUDGET_CHARS,
    STRATEGY_CHUNKED,
    STRATEGY_DIRECT,
    STRATEGY_RELEVANCE,
    call_budget_chars,
    plan_input,
    plan_reduced,
)
from system.ai.text_optimization.settings import TextOptimizationSettings
from tests.contract_samples import (
    FOOTER_TEMPLATE,
    HEADER,
    NOTICE_AT_END,
    PRICE_TABLE,
    RELEVANT_SNIPPETS,
    build_contract,
    filler_section,
)

SCHEMA = MODES["contract_extraction"].response_schema
SETTINGS = TextOptimizationSettings()
# Per-call budgets as computed for the real v1 prompt: Ollama's default
# 4096-token window, and a profile with context_window_tokens: 8192.
_TEMPLATE = open("system/prompts/contract_extraction/v1.txt", encoding="utf-8").read()
_OVERHEAD = len(build_prompt(_TEMPLATE, {"document_type": "contract"}, ""))
SMALL_BUDGET = call_budget_chars(4096, _OVERHEAD)
LARGE_BUDGET = call_budget_chars(8192, _OVERHEAD)


def _plan(text: str, budget: int = LARGE_BUDGET, settings: TextOptimizationSettings = SETTINGS):
    return plan_input(text, CONTRACT_GROUPS, settings, budget)


def _all_text(plan) -> str:
    return "\n".join(call.text for call in plan.calls)


# --- Field groups vs. schema ----------------------------------------------


def test_field_groups_cover_exactly_the_schema_except_notes():
    grouped = [field for group in CONTRACT_GROUPS for field in group.fields]
    assert len(grouped) == len(set(grouped)), "a field must belong to exactly one group"
    assert set(grouped) | {"notes"} == set(SCHEMA["properties"])


def test_dependent_fields_exist_in_schema():
    for dependent, required in CONTRACT_DEPENDENT_FIELDS.items():
        assert dependent in SCHEMA["properties"]
        assert required in SCHEMA["properties"]


# --- Phase A: normalization ------------------------------------------------


def test_whitespace_and_blank_lines_are_collapsed():
    raw = "  Vertrag   über\tWartung  \r\n\r\n\r\n\r\n\nZweiter Absatz hier  \n"
    assert normalize_text(raw) == "Vertrag  über  Wartung\n\nZweiter Absatz hier"


def test_invisible_characters_are_removed():
    assert normalize_text("Kündi­gungs​frist") == "Kündigungsfrist"


def test_explicit_page_numbers_are_removed_but_bare_numbers_without_page_info_stay():
    raw = "Text A\nSeite 2 von 7\nText B\n- 3 -\nPage 4 of 9\nLaufzeit (Monate)\n24"
    assert normalize_text(raw) == "Text A\nText B\nLaufzeit (Monate)\n24"


def test_bare_page_number_at_page_edge_is_removed_when_page_breaks_are_known():
    raw = "Erste Seite Inhalt\n1\fZweite Seite Inhalt\n2"
    normalized = normalize_text(raw)
    assert "Erste Seite Inhalt" in normalized and "Zweite Seite Inhalt" in normalized
    assert "\n1\n" not in f"\n{normalized}\n" and "\n2\n" not in f"\n{normalized}\n"


def test_repeated_header_and_footer_are_reduced_to_first_occurrence_with_page_breaks():
    raw = build_contract(12000, pages=5)
    normalized = normalize_text(raw)
    assert normalized.count(HEADER) == 1
    assert normalized.count("Seite") == 1  # first footer kept (carries the contract number)
    assert raw.count(HEADER) == 5  # raw input untouched


def test_repeated_header_without_page_breaks_needs_wide_spacing():
    sections = [filler_section(i, 1200) for i in range(4)]
    raw = "\n\n".join(f"Allgemeine Vertragsbedingungen\n{section}" for section in sections)
    assert normalize_text(raw).count("Allgemeine Vertragsbedingungen") == 1


def test_identical_adjacent_table_rows_are_not_treated_as_header():
    rows = "\n".join(["Wartung Aufzug  |  450,00 €  |  monatlich"] * 4)
    raw = f"§ 3 Vergütung\n{rows}\nEnde der Tabelle"
    assert normalize_text(raw).count("450,00 €") == 4


def test_section_headings_at_page_tops_are_not_removed():
    pages = [f"§ {n} Bestimmungen Teil {n}\nInhalt der Bestimmung {n}." for n in range(1, 6)]
    normalized = normalize_text("\f".join(pages))
    for n in range(1, 6):
        assert f"§ {n} Bestimmungen Teil {n}" in normalized


def test_symbol_only_scan_noise_is_removed_but_meaningful_symbols_stay():
    raw = "Vertrag\n-----------\n_ _ _ _ _ _\n|||\n☒ monatlich\n§\n€ 450,00\nEnde"
    assert normalize_text(raw) == "Vertrag\n☒ monatlich\n§\n€ 450,00\nEnde"


def test_consecutive_ocr_duplicate_lines_are_removed_but_short_cells_stay():
    long_line = "Der Vertrag verlängert sich um jeweils zwölf Monate."
    raw = f"{long_line}\n{long_line}\nmonatlich\nmonatlich"
    assert normalize_text(raw) == f"{long_line}\nmonatlich\nmonatlich"


def test_later_exact_copy_of_long_paragraph_is_removed_shorter_repeats_stay():
    paragraph = "Die Haftung richtet sich nach den gesetzlichen Vorschriften. " * 5
    raw = f"{paragraph}\n\nZwischentext\n\n{paragraph}\n\nKurz.\n\nKurz."
    normalized = normalize_text(raw)
    assert normalized.count(paragraph.strip()) == 1
    assert normalized.count("Kurz.") == 2


def test_hyphenation_across_line_break_is_repaired():
    assert normalize_text("Die Kündigungs-\nfrist beträgt drei Monate.") == (
        "Die Kündigungsfrist beträgt drei Monate."
    )


def test_hyphen_before_conjunction_and_capitalized_word_is_kept():
    assert normalize_text("Wartungs-\nund Instandhaltungsarbeiten") == "Wartungs- und Instandhaltungsarbeiten"
    assert normalize_text("Der Service-\nVertrag gilt") == "Der Service-Vertrag gilt"


def test_sentence_wrapped_mid_sentence_is_joined():
    raw = "Der Vertrag verlängert sich jeweils\num 12 Monate, wenn er nicht gekündigt wird."
    assert (
        normalize_text(raw)
        == "Der Vertrag verlängert sich jeweils um 12 Monate, wenn er nicht gekündigt wird."
    )


def test_paragraphs_headings_and_list_items_keep_their_structure():
    raw = (
        "§ 4 Laufzeit\nDer Vertrag beginnt am 01.01.2026.\n\n"
        "Pflichten des Auftragnehmers sind insbesondere\na) die Wartung,\nb) die Dokumentation."
    )
    normalized = normalize_text(raw)
    assert normalized.split("\n\n")[0] == "§ 4 Laufzeit\nDer Vertrag beginnt am 01.01.2026."
    assert "\na) die Wartung,\nb) die Dokumentation." in normalized


def test_tables_are_not_destroyed():
    normalized = normalize_text(PRICE_TABLE)
    assert "Leistung  |  Preis  |  Intervall\nWartung Aufzug 1  |  450,00 €  |  monatlich" in normalized
    column_table = "Leistung        Preis       Intervall\nWartung         450 €       monatlich"
    assert normalize_text(column_table) == "Leistung  Preis  Intervall\nWartung  450 €  monatlich"


def test_normalization_never_adds_words():
    raw = build_contract(18000, pages=6, dense=True)
    raw_words = set(raw.replace("\f", " ").split())
    for word in normalize_text(raw).split():
        assert word in raw_words or word == "...", word


# --- Phase B: passage selection --------------------------------------------


def _select_all(text: str, budget: int):
    blocks = segment_blocks(text)
    scores = score_blocks(text, blocks, CONTRACT_GROUPS)
    names = tuple(group.name for group in CONTRACT_GROUPS)
    return select(text, blocks, scores, names, budget, head_chars=1500, tail_chars=600)


def test_selected_passages_are_verbatim_substrings_of_the_normalized_text():
    normalized = normalize_text(build_contract(25000, dense=True))
    selection = _select_all(normalized, 5000)
    for part in selection.text.split("\n\n"):
        if not part.startswith("[Excerpt"):
            assert part in normalized
    for excerpt in selection.text.split("[Excerpt")[1:]:
        body = excerpt.split("]\n", 1)[1].strip()
        assert body in normalized


def test_notice_renewal_price_number_and_scope_are_found():
    normalized = normalize_text(build_contract(30000, dense=True))
    selection = _select_all(normalized, 4000)
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in selection.text, snippet
    assert selection.complete


def test_english_contract_passages_are_found():
    filler = "\n\n".join(filler_section(i, 1400) for i in range(10, 20))
    text = (
        "SERVICE AGREEMENT\nContract No. SA-77\nbetween Example Hotel Ltd and Sample Services Ltd\n\n"
        f"{filler}\n\n"
        "4. Term\nThe agreement shall commence on January 1, 2026 and has an initial term of 24 months.\n\n"
        f"{filler}\n\n"
        "7. Fees\nThe monthly fee is EUR 450 per month, payable in advance.\n\n"
        f"{filler}\n\n"
        "9. Termination\nEither party may terminate with a notice period of three months. "
        "The agreement renews automatically for successive 12-month periods."
    )
    selection = _select_all(normalize_text(text), 4000)
    for snippet in (
        "SA-77",
        "commence on January 1, 2026",
        "EUR 450 per month",
        "notice period of three months",
    ):
        assert snippet in selection.text, snippet


def test_overlapping_windows_are_merged_without_duplicate_passages():
    text = normalize_text(build_contract(20000, dense=True))
    selection = _select_all(text, 6000)
    excerpts = [part.split("]\n", 1)[1] for part in selection.text.split("[Excerpt")[1:]]
    assert len(excerpts) == len(set(excerpts)) == selection.passage_count
    assert selection.text.count("§ 4 Laufzeit") == 1
    assert selection.text.count("§ 3 Vergütung") == 1


def test_price_table_stays_together_in_selection():
    selection = _select_all(normalize_text(build_contract(20000, dense=True)), 4000)
    assert PRICE_TABLE in selection.text


def test_document_head_is_always_kept():
    selection = _select_all(normalize_text(build_contract(30000)), MIN_CALL_BUDGET_CHARS)
    assert selection.text.startswith("[Excerpt 1/")
    assert "Vertragsnummer: WV-2026-0815" in selection.text


def test_no_keyword_document_keeps_head_tail_and_a_spread_sample():
    sections = [f"Abschnitt {i}\n" + ("Lorem ipsum dolor sit amet. " * 40) for i in range(30)]
    text = normalize_text("\n\n".join(sections))
    selection = _select_all(text, 6000)
    assert "Abschnitt 0" in selection.text  # head
    assert "Abschnitt 29" in selection.text  # tail
    assert "Abschnitt 15" in selection.text  # midpoint sample
    assert 3000 < len(selection.text) <= 6000


def test_selection_respects_budget():
    text = normalize_text(build_contract(60000, dense=True))
    for budget in (MIN_CALL_BUDGET_CHARS, 3000, 5000, 9000):
        assert len(_select_all(text, budget).text) <= budget


def test_segmentation_splits_wall_of_text_without_newlines():
    text = "Ein Satz ohne Umbruch. " * 400
    blocks = segment_blocks(text)
    assert len(blocks) > 3
    assert all(end - start <= 1400 for start, end in blocks)
    assert "".join(text[s:e] for s, e in blocks).replace(" ", "") == text.replace(" ", "")


# --- Planning / thresholds --------------------------------------------------


def test_small_contract_is_sent_directly_and_complete():
    raw = build_contract(5000)
    plan = _plan(raw)
    assert plan.strategy == STRATEGY_DIRECT
    assert len(plan.calls) == 1
    assert plan.calls[0].text == normalize_text(raw)
    assert plan.stats["chunk_count"] == 0


def test_3k_contract_is_direct_even_with_the_small_default_window():
    plan = _plan(build_contract(3000), budget=SMALL_BUDGET)
    assert plan.strategy == STRATEGY_DIRECT


def test_10k_contract_is_reduced_but_keeps_relevant_clauses():
    plan = _plan(build_contract(10000, dense=True))
    assert plan.strategy == STRATEGY_RELEVANCE
    assert plan.stats["optimized_chars"] < plan.stats["normalized_chars"]
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in _all_text(plan), snippet


def test_18k_contract_is_reduced_to_the_target_range():
    plan = _plan(build_contract(17763, pages=6, dense=True))
    assert plan.strategy == STRATEGY_RELEVANCE
    assert plan.stats["raw_chars"] > 17000
    assert plan.stats["optimized_chars"] <= SETTINGS.target_chars
    assert plan.stats["optimized_chars"] < plan.stats["normalized_chars"] / 2
    for snippet in RELEVANT_SNIPPETS:
        assert snippet in _all_text(plan), snippet


def test_notice_clause_at_the_end_of_30k_filler_is_kept():
    plan = _plan(build_contract(30000, dense=True), budget=SMALL_BUDGET)
    assert "Kündigungsfrist: 3 Monate zum Vertragsende" in _all_text(plan)
    assert NOTICE_AT_END in _all_text(plan)


def test_renewal_in_the_middle_is_kept():
    plan = _plan(build_contract(30000, dense=True), budget=SMALL_BUDGET)
    assert "verlängert sich automatisch um jeweils weitere 12 Monate" in _all_text(plan)


def _many_clauses_contract(copies: int) -> str:
    """Lots of distinct significant clauses (more than one call can hold)."""
    parts = [build_contract(4000)]
    for i in range(copies):
        parts.append(
            f"§ {20 + i} Preisanpassung Nr. {i}\nDie Vergütung für Leistungspaket {i} beträgt {100 + i},00 € "
            f"und ist quartalsweise fällig.\n\n"
            f"§ {60 + i} Sonderkündigung Nr. {i}\nEine Kündigung von Leistungspaket {i} ist mit einer Frist "
            f"von {i + 1} Wochen möglich; die Laufzeit verlängert sich sonst um {i + 1} Monate."
        )
        parts.append(filler_section(100 + i, 1200))
    return "\n\n".join(parts)


def test_very_long_contract_with_more_relevant_text_than_one_call_is_chunked():
    plan = _plan(_many_clauses_contract(25), budget=SMALL_BUDGET)
    assert plan.strategy == STRATEGY_CHUNKED
    assert 2 <= plan.stats["chunk_count"] <= len(CONTRACT_GROUPS)
    labels = [call.label for call in plan.calls]
    assert labels[0] == "basis"
    for call in plan.calls:
        assert len(call.text) <= SMALL_BUDGET
        assert call.fields is not None
    term = next(call for call in plan.calls if call.label == "term")
    finance = next(call for call in plan.calls if call.label == "finance")
    assert "Sonderkündigung" in term.text
    assert "Preisanpassung" in finance.text
    # Not the full document per chunk:
    assert all(len(call.text) < plan.stats["normalized_chars"] / 2 for call in plan.calls)


def test_chunking_disabled_falls_back_to_relevance_with_dropped_passages_reported():
    settings = TextOptimizationSettings(chunking_enabled=False)
    plan = _plan(_many_clauses_contract(25), budget=SMALL_BUDGET, settings=settings)
    assert plan.strategy == STRATEGY_RELEVANCE
    assert plan.stats["dropped_passages"] > 0
    assert len(plan.calls[0].text) <= SMALL_BUDGET


def test_direct_limit_is_capped_by_the_call_budget():
    raw = build_contract(6000)
    assert _plan(raw, budget=LARGE_BUDGET).strategy == STRATEGY_DIRECT
    assert _plan(raw, budget=SMALL_BUDGET).strategy == STRATEGY_RELEVANCE


def test_stats_contain_sizes_strategy_and_timings_but_no_text():
    plan = _plan(build_contract(18000, dense=True))
    assert set(plan.stats) >= {
        "raw_chars",
        "normalized_chars",
        "optimized_chars",
        "strategy",
        "passage_count",
        "chunk_count",
        "dropped_passages",
        "normalization_ms",
        "optimization_ms",
    }
    for value in plan.stats.values():
        assert not isinstance(value, str) or value in (STRATEGY_DIRECT, STRATEGY_RELEVANCE, STRATEGY_CHUNKED)


def test_reduced_fallback_plan_is_smaller_and_keeps_the_head():
    plan = _plan(build_contract(18000, dense=True))
    reduced = plan_reduced(plan.normalized_text, CONTRACT_GROUPS, SETTINGS, len(plan.calls[0].text) // 2)
    assert len(reduced.text) <= len(plan.calls[0].text) // 2
    assert "WV-2026-0815" in reduced.text


def test_raw_text_is_never_modified():
    raw = build_contract(20000, pages=6, dense=True)
    copy = str(raw)
    _plan(raw, budget=SMALL_BUDGET)
    assert raw == copy


# --- Budget ------------------------------------------------------------------


def test_call_budget_leaves_room_for_prompt_output_and_margin():
    assert SMALL_BUDGET < 4096 * 3.2 - _OVERHEAD
    assert LARGE_BUDGET > SMALL_BUDGET
    assert call_budget_chars(2048, 50_000) == MIN_CALL_BUDGET_CHARS


# --- Merge -------------------------------------------------------------------


def _empty() -> dict:
    return {field: None for field in SCHEMA["properties"]}


def test_merge_takes_owner_values_and_ignores_non_owner_guesses():
    basis = {**_empty(), "vendor_name": "Musterlift GmbH", "amount": 999.0}
    term = {**_empty(), "start_date": "2026-01-01", "vendor_name": "Andere GmbH"}
    finance = {**_empty(), "amount": 450.0, "currency": "EUR", "payment_interval": "monthly"}
    groups = {g.name: g.fields for g in CONTRACT_GROUPS}
    merged, conflicts = merge_results(
        [(groups["basis"], basis), (groups["term"], term), (groups["finance"], finance)],
        SCHEMA,
        CONTRACT_DEPENDENT_FIELDS,
    )
    assert merged["vendor_name"] == "Musterlift GmbH"
    assert merged["start_date"] == "2026-01-01"
    assert merged["amount"] == 450.0 and merged["currency"] == "EUR"
    assert conflicts == []
    assert merged["notes"] is None


def test_merge_conflict_is_nulled_and_noted_never_picked():
    a = {**_empty(), "end_date": "2027-12-31", "notes": "Preisgleitklausel vorhanden."}
    b = {**_empty(), "end_date": "2028-06-30", "notes": "Preisgleitklausel vorhanden. "}
    fields = ("end_date",)
    merged, conflicts = merge_results([(fields, a), (fields, b)], SCHEMA, CONTRACT_DEPENDENT_FIELDS)
    assert merged["end_date"] is None
    assert conflicts == ["end_date"]
    assert merged["notes"].startswith("Preisgleitklausel vorhanden.")
    assert merged["notes"].count("Preisgleitklausel") == 1
    assert '"2027-12-31" / "2028-06-30"' in merged["notes"]


def test_merge_same_value_in_different_spelling_is_not_a_conflict():
    a = {**_empty(), "vendor_name": "Musterlift  GmbH", "amount": 450}
    b = {**_empty(), "vendor_name": "musterlift gmbh", "amount": 450.0}
    merged, conflicts = merge_results([(None, a), (None, b)], SCHEMA, {})
    assert conflicts == []
    assert merged["vendor_name"] == "Musterlift  GmbH"
    assert merged["amount"] == 450


def test_merge_field_without_any_owner_chunk_falls_back_to_all_chunks():
    basis = {**_empty(), "vendor_name": "Musterlift GmbH", "amount": 450.0, "currency": "EUR"}
    basis_fields = next(g.fields for g in CONTRACT_GROUPS if g.name == "basis")
    merged, _ = merge_results([(basis_fields, basis)], SCHEMA, CONTRACT_DEPENDENT_FIELDS)
    assert merged["amount"] == 450.0 and merged["currency"] == "EUR"


def test_merge_dependent_fields_are_nulled_never_filled():
    a = {**_empty(), "notice_period_unit": "months", "currency": "EUR", "renewal_period_value": 12}
    merged, _ = merge_results([(None, a)], SCHEMA, CONTRACT_DEPENDENT_FIELDS)
    assert merged["notice_period_unit"] is None
    assert merged["currency"] is None
    assert merged["renewal_period_value"] is None


def test_merge_result_has_exactly_the_schema_keys():
    merged, _ = merge_results([(None, _empty())], SCHEMA, CONTRACT_DEPENDENT_FIELDS)
    assert set(merged) == set(SCHEMA["properties"])


# --- Performance -------------------------------------------------------------


def test_optimizer_is_fast_and_roughly_linear_on_100k_characters():
    raw_100k = build_contract(100_000, pages=35, dense=True)
    raw_25k = build_contract(25_000, pages=9, dense=True)
    started = time.perf_counter()
    plan = _plan(raw_100k, budget=SMALL_BUDGET)
    elapsed_100k = time.perf_counter() - started
    started = time.perf_counter()
    _plan(raw_25k, budget=SMALL_BUDGET)
    elapsed_25k = time.perf_counter() - started
    assert len(raw_100k) >= 100_000
    assert elapsed_100k < 2.0
    # 4x the input must not cost anything like 16x (quadratic) the time.
    assert elapsed_100k < max(elapsed_25k, 0.01) * 10
    assert plan.stats["optimized_chars"] <= SMALL_BUDGET * len(CONTRACT_GROUPS)


def test_footer_template_is_detected_as_page_footer():
    # Sanity check for the sample itself: footer lines carry a page token.
    assert "Seite" in FOOTER_TEMPLATE


def test_contradictory_passages_both_reach_the_model():
    # The optimizer must not resolve a contradiction by dropping one side -
    # the prompt's own rule (null + notes) can only work if both are sent.
    text = build_contract(9000, dense=True).replace(
        "Kündigungsfrist: 3 Monate zum Vertragsende.",
        "Kündigungsfrist: 3 Monate zum Vertragsende.\n\n§ 15 Abweichende Regelung\n"
        "Abweichend von § 14 beträgt die Kündigungsfrist 6 Monate zum Vertragsende.",
    )
    plan = _plan(text, budget=SMALL_BUDGET)
    sent = _all_text(plan)
    assert "Kündigungsfrist: 3 Monate" in sent
    assert "Kündigungsfrist 6 Monate" in sent
