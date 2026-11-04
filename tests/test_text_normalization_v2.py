"""Normalization V2 regression tests (contract text optimization, Phase A).

Pure functions only. All texts are synthetic (invented companies, numbers,
people - see tests/contract_samples.py). The central invariants:

- technical ballast (whitespace, page numbers, headers/footers, rule lines,
  OCR double layers, TOC runs, PDF line wraps) is removed or reshaped;
- every business-relevant value survives, tables and list structure stay;
- nothing is invented: the output's visible characters are a subsequence
  of the input's (see _assert_no_invention).
"""

from __future__ import annotations

import time

from system.ai.text_optimization.contract import CONTRACT_GROUPS
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.planner import plan_input
from system.ai.text_optimization.settings import TextOptimizationSettings
from tests.contract_samples import (
    PDF_HEADER_LINES,
    PDF_OCR_DOUBLE,
    PDF_RELEVANT_SNIPPETS,
    build_pdf_contract,
    filler_section,
)

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}


def _visible(text: str) -> str:
    for ligature, letters in _LIGATURES.items():
        text = text.replace(ligature, letters)
    # Leaders become "..." - compare underscores/middle dots as dots.
    return "".join(ch for ch in text.replace("_", ".").replace("·", ".") if not ch.isspace())


def _assert_no_invention(raw: str, normalized: str) -> None:
    """Every visible output character occurs in the input, in order - the
    normalizer only deletes, reshapes whitespace and joins lines."""
    source = iter(_visible(raw))
    output = _visible(normalized)
    for position, ch in enumerate(output):
        if not any(ch == candidate for candidate in source):
            raise AssertionError(f"invented/reordered text at {position}: {output[position:][:60]!r}")


def _norm(raw: str) -> str:
    normalized = normalize_text(raw)
    _assert_no_invention(raw, normalized)
    return normalized


def _pages(*pages: str) -> str:
    return "\f".join(pages)


# --- 1./2. whitespace and blank lines ------------------------------------------


def test_whitespace_variants_are_reduced_but_column_gaps_stay_visible():
    raw = "  Vertrag über die　Wartung   und\t\tPflege  \nLeistung\t450 €"
    assert _norm(raw) == "Vertrag über die Wartung  und  Pflege\nLeistung  450 €"


def test_blank_and_whitespace_only_lines_collapse_to_one_paragraph_break():
    raw = "Erster Absatz.\n \n\t\n \n\n\n\nZweiter Absatz.\n\n\n"
    assert _norm(raw) == "Erster Absatz.\n\nZweiter Absatz."


def test_invisible_control_and_ligature_characters_are_cleaned():
    raw = "Pﬂichten des Auftrag​nehmers\x07 und Zuschlag\x00 Kündi­gung"
    assert normalize_text(raw) == "Pflichten des Auftragnehmers und Zuschlag\nKündigung"


def test_unicode_line_separators_become_line_breaks():
    assert normalize_text("Zeile A Zeile B\vZeile C\r\nZeile D") == "Zeile A\nZeile B\nZeile C\nZeile D"


# --- 3. PDF line wraps --------------------------------------------------------


def test_pdf_running_text_with_visual_line_breaks_becomes_one_paragraph():
    raw = (
        "Der Vertrag verlängert sich automatisch\n"
        "um jeweils zwölf Monate, sofern er nicht\n"
        "mit einer Frist von drei Monaten gekündigt wird."
    )
    assert _norm(raw) == (
        "Der Vertrag verlängert sich automatisch um jeweils zwölf Monate, sofern er nicht "
        "mit einer Frist von drei Monaten gekündigt wird."
    )


def test_german_wrap_before_capitalized_noun_is_joined_inside_running_text():
    raw = (
        "Gegenstand dieses Vertrages ist die regelmäßige Wartung und Inspektion der\n"
        "Aufzugsanlagen des Auftraggebers durch den Auftragnehmer gemäß Anlage 1.\n"
        "Der Auftragnehmer setzt ausschließlich geschultes Personal ein. Der\n"
        "Auftraggeber stellt die erforderlichen Zugänge zur Verfügung. Laufzeit von 24\n"
        "Monaten."
    )
    assert _norm(raw) == (
        "Gegenstand dieses Vertrages ist die regelmäßige Wartung und Inspektion der "
        "Aufzugsanlagen des Auftraggebers durch den Auftragnehmer gemäß Anlage 1.\n"
        "Der Auftragnehmer setzt ausschließlich geschultes Personal ein. Der "
        "Auftraggeber stellt die erforderlichen Zugänge zur Verfügung. Laufzeit von 24 Monaten."
    )


def test_line_after_a_sentence_end_or_capitalized_last_word_keeps_its_break():
    raw = (
        "Die Leistungen werden durch den Auftragnehmer vollständig erbracht.\n"
        "Die Abrechnung erfolgt durch die Buchhaltung des Auftraggebers.\n"
        "Hotel Beispielhof GmbH, Am Markt 1, 54321 Beispielstadt, vertreten durch\n"
        "Musterlift Aufzugsservice GmbH"
    )
    assert _norm(raw) == raw


# --- 4. lists ------------------------------------------------------------------


def test_lists_are_not_joined():
    raw = (
        "Pflichten des Auftragnehmers sind insbesondere die folgenden Leistungen\n"
        "a) die Wartung,\n"
        "b) die Dokumentation,\n"
        "- Prüfung der Notrufeinrichtung\n"
        "• Austausch von Verschleißteilen\n"
        "1. Inspektion\n"
        "Wartung der Aufzugsanlage\n"
        "Prüfung der Sicherheitseinrichtungen"
    )
    assert _norm(raw) == raw


# --- 5. tables -----------------------------------------------------------------


def test_tables_are_not_joined_or_compressed_into_running_text():
    raw = (
        "Leistung              Preis          Intervall\n"
        "Wartung Aufzug 1      450,00 €       monatlich\n"
        "Leistung | Preis | Intervall\n"
        "Notdienst | 1.200 EUR | jährlich"
    )
    assert _norm(raw) == (
        "Leistung  Preis  Intervall\n"
        "Wartung Aufzug 1  450,00 €  monatlich\n"
        "Leistung | Preis | Intervall\n"
        "Notdienst | 1.200 EUR | jährlich"
    )


def test_table_with_one_cell_per_line_stays_one_cell_per_line():
    raw = "Leistung\nPreis\nIntervall\nWartung Aufzug 1\n450,00 €\nmonatlich\nNotdienst\n1.200 EUR\njährlich"
    assert _norm(raw) == raw


def test_rule_line_inside_a_table_or_text_is_removed_without_merging_neighbors():
    raw = (
        "Die Wartung umfasst die folgenden Arbeiten an der Anlage des Auftraggebers\n"
        "------------------------------------------\n"
        "monatliche Sichtprüfung aller Sicherheitseinrichtungen\n"
        "Wartung Aufzug 1\n"
        "=============\n"
        "monatlich"
    )
    assert _norm(raw) == (
        "Die Wartung umfasst die folgenden Arbeiten an der Anlage des Auftraggebers\n"
        "monatliche Sichtprüfung aller Sicherheitseinrichtungen\n"
        "Wartung Aufzug 1\n"
        "monatlich"
    )


def test_symbol_runs_inside_a_line_are_shortened_but_meaningful_symbols_stay():
    raw = "Preis ============ 450,00 €\nName: ______________\n☐ ja ☒ nein ✓\n§\n%\n$"
    assert _norm(raw) == "Preis === 450,00 €\nName: ...\n☐ ja ☒ nein ✓\n§\n%\n$"


# --- 6. headings ---------------------------------------------------------------


def test_headings_titles_and_labels_are_kept_on_their_own_line():
    raw = (
        "WARTUNGSVERTRAG FÜR AUFZUGSANLAGEN UND FAHRTREPPEN IM GEBÄUDE DES\n"
        "AUFTRAGGEBERS\n"
        "§ 4 Laufzeit\n"
        "Der Vertrag beginnt mit Unterzeichnung durch beide Vertragsparteien und\n"
        "Vertragsnummer: WV-2026-0815\n"
        "4. Term and termination of this agreement between the parties named\n"
        "The agreement commences on January 1, 2026."
    )
    assert _norm(raw) == raw


# --- 7./8. hyphenation ---------------------------------------------------------


def test_hyphenated_word_across_line_break_is_rejoined():
    assert _norm("Die Kündigungs-\nfrist beträgt drei Monate.") == "Die Kündigungsfrist beträgt drei Monate."


def test_suspended_compound_before_conjunction_is_not_merged():
    assert _norm("Wartungs- und\nServiceleistungen") == "Wartungs- und\nServiceleistungen"
    long_line = "Der Auftragnehmer erbringt die vereinbarten Wartungs- und"
    assert _norm(f"{long_line}\nServiceleistungen gemäß Anlage 2.") == (
        f"{long_line} Serviceleistungen gemäß Anlage 2."
    )
    assert _norm("Wartungs-\nund Serviceleistungen") == "Wartungs- und Serviceleistungen"
    assert _norm("Montags-\nbis Freitags") == "Montags- bis Freitags"


def test_real_hyphen_words_are_kept():
    assert _norm("Der Service-\nVertrag gilt") == "Der Service-Vertrag gilt"
    assert _norm("Der Service-Vertrag gilt EU-\nweit") == "Der Service-Vertrag gilt EU-weit"


# --- 9.-12. headers, footers, page numbers ---------------------------------------


def _paged_contract(pages: int = 6) -> str:
    header = "Musterlift GmbH\nVertrag 12345"
    bodies = [
        f"§ {n} Bestimmung {n}\n{filler_section(n, 700)}\nDie Vergütung für Teil {n} beträgt {n}00,00 €."
        for n in range(1, pages + 1)
    ]
    return _pages(*(f"{header}\n\n{body}\n\nSeite {i + 1} von {pages}" for i, body in enumerate(bodies)))


def test_repeated_header_block_is_kept_once_with_its_business_info():
    raw = _paged_contract()
    normalized = _norm(raw)
    assert normalized.count("Musterlift GmbH") == 1
    assert normalized.count("Vertrag 12345") == 1
    assert normalized.startswith("Musterlift GmbH\nVertrag 12345")  # first instance kept
    for n in range(1, 7):
        assert f"§ {n} Bestimmung {n}" in normalized  # section headings are not headers
        assert f"{n}00,00 €" in normalized


def test_footer_with_page_counter_is_matched_digit_insensitively():
    pages = [
        f"Inhalt Teil {n}.\n{filler_section(n, 400)}\nMusterlift GmbH | Vertrag 12345 | {n}/5"
        for n in range(1, 6)
    ]
    normalized = _norm(_pages(*pages))
    assert normalized.count("Musterlift GmbH | Vertrag 12345") == 1


def test_footer_without_page_breaks_is_reduced_to_first_occurrence():
    sections = [filler_section(i, 1200) for i in range(4)]
    raw = "\n".join(
        f"Musterlift GmbH · Vertrag 12345\n{section}\nSeite {i + 1} von 4"
        for i, section in enumerate(sections)
    )
    normalized = _norm(raw)
    assert normalized.count("Musterlift GmbH · Vertrag 12345") == 1
    assert "Seite" not in normalized


def test_explicit_page_number_variants_are_removed():
    variants = ["Seite 4", "Seite 4 von 12", "Seite: 4", "Seite 4/12", "S. 4 von 12", "Page 4 of 12",
                "page 4/12", "- 4 -", "– 4 –", "4 von 12", "4 of 12", "Blatt 2"]  # fmt: skip
    for variant in variants:
        assert _norm(f"Text davor\n{variant}\nText danach") == "Text davor\nText danach", variant


def test_sentence_split_by_a_page_break_is_rejoined():
    raw = _pages(
        "Der Vertrag verlängert sich automatisch um jeweils zwölf Monate, sofern er nicht\nSeite 1 von 2",
        "mit einer Frist von drei Monaten gekündigt wird.\nSeite 2 von 2",
    )
    assert _norm(raw) == (
        "Der Vertrag verlängert sich automatisch um jeweils zwölf Monate, sofern er nicht "
        "mit einer Frist von drei Monaten gekündigt wird."
    )


def test_page_break_after_a_finished_paragraph_stays_a_paragraph_break():
    first = "§ 1 Gegenstand\nDie Wartung der Anlagen."
    second = "§ 2 Laufzeit\nDer Vertrag beginnt am 01.01.2026."
    assert _norm(_pages(first, second)) == f"{first}\n\n{second}"


# --- 13. bare numbers ---------------------------------------------------------------


def test_bare_numbers_in_the_body_are_never_removed():
    raw = "Laufzeit (Monate)\n24\nKundennummer\n4711\nAnteil\n3/7\n12"
    assert _norm(raw) == raw


def test_page_sequence_numbers_at_page_edges_are_removed_other_edge_numbers_stay():
    values = ("4711", "815", "24", "99")
    pages = [f"Inhalt Teil {n}\nWert {n}\n{values[n - 1]}\n{n}" for n in range(1, 5)]
    lines = _norm(_pages(*pages)).split("\n")
    for n, value in enumerate(values, start=1):
        assert f"Inhalt Teil {n}" in lines
        assert value in lines  # a value at the edge that is not the page sequence
    for page_number in ("1", "2", "3", "4"):
        assert page_number not in lines


def test_edge_number_that_does_not_follow_the_page_sequence_is_kept():
    pages = [f"Laufzeit in Monaten\n{value}" for value in (24, 24, 36)]
    assert _norm(_pages(*pages)).count("\n24") == 2


# --- 14. OCR duplicates ------------------------------------------------------------


def test_exact_ocr_duplicate_lines_are_removed_even_with_spacing_differences():
    line = "Der Vertrag verlängert sich automatisch um 12 Monate."
    assert _norm(f"{line}\n{line}") == line
    assert _norm(f"{line}\n{line.replace(' um ', '  um ')}") == line


def test_directly_repeated_paragraph_is_removed():
    paragraph = "Die Kündigung bedarf zu ihrer Wirksamkeit der Schriftform."
    assert _norm(f"{paragraph}\n\n{paragraph}\n\nWeiter.") == f"{paragraph}\n\nWeiter."


def test_near_duplicates_with_different_values_are_both_kept():
    # A contradiction must reach the model - no near-duplicate removal.
    a = "Der Vertrag verlängert sich automatisch um 12 Monate."
    b = "Der Vertrag verlängert sich automatisch um 18 Monate."
    assert _norm(f"{a}\n{b}\n\n{a}\n\n{b}").count("18 Monate") == 2


def test_repeated_sentence_far_apart_is_not_taken_for_a_header():
    clause = "Die Kündigung bedarf der Schriftform."
    middle = "für leichte Fahrlässigkeit ist ausgeschlossen, soweit gesetzlich zulässig"
    sections = [f"§ {i} Teil\n{filler_section(i, 1200)}\n{clause}\nDie Haftung\n{middle}" for i in range(4)]
    normalized = _norm("\n\n".join(sections))
    assert normalized.count(clause) == 4
    assert normalized.count(middle) == 4


# --- 15.-18. short business values, amounts, numbers, § -------------------------------


SHORT_VALUES = (
    "3 Monate",
    "30 Tage",
    "12 Monate",
    "6.999,00 €",
    "1.200 EUR",
    "§ 4",
    "KONE GmbH",
    "31.12.2027",
    "monatlich",
    "jährlich",
    "4711",
)


def test_short_business_values_survive_on_their_own_lines():
    raw = "\n".join(SHORT_VALUES)
    assert _norm(raw) == raw


def test_short_business_values_recurring_far_apart_are_not_taken_for_headers():
    values = [v for v in SHORT_VALUES if v != "KONE GmbH"]  # a company name may be a letterhead
    sections = [f"§ {i} Abschnitt\n{filler_section(i, 1200)}\n" + "\n".join(values) for i in range(4)]
    normalized = _norm("\n\n".join(sections))
    for value in values:
        assert normalized.count(f"\n{value}\n") + normalized.endswith(f"\n{value}") >= 4, value


def test_short_business_values_at_page_edges_are_not_taken_for_headers():
    pages = [f"31.12.2027\n12 Monate\nInhalt Teil {n} mit Text.\n6.999,00 €\njährlich" for n in range(5)]
    normalized = _norm(_pages(*pages))
    for value in ("31.12.2027", "12 Monate", "6.999,00 €", "jährlich"):
        assert normalized.count(value) == 5, value


def test_amounts_are_never_converted_or_reformatted():
    raw = "Preis: 6.999,00 € netto\nPauschale EUR 1.200\nFee: $1,200.00 per month\nRabatt 2,5 %"
    assert _norm(raw) == raw


def test_contract_numbers_dates_and_section_signs_are_kept_verbatim():
    raw = "Vertragsnummer: WV-2026-0815\nKundennr. 4711/B\nStand 31.12.2027\n§§ 3-5 gelten entsprechend.\n§ 4"
    assert _norm(raw) == raw


# --- TOC ---------------------------------------------------------------------------


_SECTIONS = (
    "§ 1 Gegenstand\nWartung der Anlagen.\n\n§ 2 Vergütung\n450,00 € monatlich.\n\n§ 3 Laufzeit\n24 Monate."
)


def test_table_of_contents_run_is_removed_but_its_sections_stay():
    raw = (
        "Inhaltsverzeichnis\n"
        "§ 1 Gegenstand ................ 1\n"
        "§ 2 Vergütung ................. 2\n"
        f"§ 3 Laufzeit . . . . . . . . . 4\n\n{_SECTIONS}"
    )
    assert _norm(raw) == f"Inhaltsverzeichnis\n\n{_SECTIONS}"


def test_leader_list_of_values_is_not_a_table_of_contents():
    raw = "Laufzeit ............ 24\nKündigungsfrist ..... 3\nVerlängerung ........ 12"
    assert _norm(raw) == "Laufzeit ... 24\nKündigungsfrist ... 3\nVerlängerung ... 12"
    numbered = "1. Laufzeit ........ 24\n2. Kündigungsfrist ... 3\n3. Verlängerung ...... 12"
    assert _norm(numbered) == "1. Laufzeit ... 24\n2. Kündigungsfrist ... 3\n3. Verlängerung ... 12"
    # Counting up like page numbers, but the titles are no section headings:
    increasing = "1. Laufzeit ........ 12\n2. Verlängerung ... 12\n3. Kündigungsfrist ...... 24"
    assert _norm(f"{increasing}\n\n§ 1 Laufzeit") == (
        "1. Laufzeit ... 12\n2. Verlängerung ... 12\n3. Kündigungsfrist ... 24\n\n§ 1 Laufzeit"
    )


# --- 19./20. DE and EN texts ---------------------------------------------------------


def test_german_pdf_contract_keeps_all_relevant_snippets_and_loses_the_ballast():
    raw = build_pdf_contract(17000, pages=6)
    normalized = _norm(raw)
    for snippet in PDF_RELEVANT_SNIPPETS:
        assert snippet in normalized, snippet
    for line in PDF_HEADER_LINES:
        assert normalized.split("\n").count(line) == 1, line
    assert "Seite" not in normalized and "____" not in normalized and "\t" not in normalized
    assert "........" not in normalized and "\n\n\n" not in normalized
    assert normalized.count(PDF_OCR_DOUBLE) == 1
    assert "Wartung Aufzug 1  450,00 €  monatlich\nWartung Aufzug 2  450,00 €  monatlich" in normalized
    assert len(normalized) < len(raw) * 0.97
    assert normalized.count("\n") < raw.count("\n") / 2


def test_english_contract_text_is_cleaned_the_same_way():
    raw = _pages(
        "SERVICE AGREEMENT\nContract No. SA-77\n\n4. Term\nThe agreement shall commence on January 1, 2026\n"
        "and has an initial term of 24 months. It renews automatically for successive\n"
        "12-month periods unless terminated with a notice period of three months.\nPage 1 of 2",
        "7. Fees\nThe monthly fee is EUR 450 per month, payable in\nadvance.\nPage 2 of 2",
    )
    assert _norm(raw) == (
        "SERVICE AGREEMENT\nContract No. SA-77\n\n4. Term\nThe agreement shall commence on January 1, 2026 "
        "and has an initial term of 24 months. It renews automatically for successive\n"
        "12-month periods unless terminated with a notice period of three months.\n\n"
        "7. Fees\nThe monthly fee is EUR 450 per month, payable in advance."
    )


# --- 21./22. large documents, no invention, raw untouched ------------------------------


def test_large_pdf_contract_is_normalized_fast_linearly_and_keeps_its_clauses():
    raw_100k = build_pdf_contract(100_000, pages=33)
    raw_25k = build_pdf_contract(25_000, pages=9)
    assert len(raw_100k) >= 100_000
    started = time.perf_counter()
    normalized = normalize_text(raw_100k)
    elapsed_100k = time.perf_counter() - started
    started = time.perf_counter()
    normalize_text(raw_25k)
    elapsed_25k = time.perf_counter() - started
    assert elapsed_100k < 1.0
    assert elapsed_100k < max(elapsed_25k, 0.005) * 10  # 4x input, far from 16x (quadratic)
    _assert_no_invention(raw_100k, normalized)
    for snippet in PDF_RELEVANT_SNIPPETS:
        assert snippet in normalized, snippet


def test_wall_of_text_paragraph_without_blank_lines_stays_linear():
    raw = "\n".join(["der Auftragnehmer erbringt die vereinbarten Leistungen sorgfältig und"] * 1500)
    started = time.perf_counter()
    normalized = normalize_text(raw)
    assert time.perf_counter() - started < 0.5
    assert normalized.count("\n") == 0


def test_normalization_never_invents_or_reorders_text_on_messy_input():
    raw = _pages(
        "Musterlift GmbH\nVertrag 12345\n\nWARTUNGSVERTRAG\n§ 1 Gegenstand\nDie Kündigungs-\nfrist\n"
        "beträgt 3 Monate. Pﬂicht und​Recht ____ ... -----\n1",
        "Musterlift GmbH\nVertrag 12345\nder Rest des Satzes folgt hier\n|||||||\n"
        "Preis | 450,00 € | monatlich\n2",
        "Musterlift GmbH\nVertrag 12345\nEnde.\n3",
    )
    normalized = _norm(raw)
    assert "Kündigungsfrist" in normalized and "450,00 €" in normalized


def test_raw_text_is_not_mutated_and_planner_keeps_the_relevant_clauses():
    raw = build_pdf_contract(30_000, pages=10)
    copy = str(raw)
    plan = plan_input(raw, CONTRACT_GROUPS, TextOptimizationSettings(), 15_000)
    assert raw == copy
    assert plan.stats["raw_chars"] == len(raw)
    sent = "\n".join(call.text for call in plan.calls)
    for snippet in PDF_RELEVANT_SNIPPETS:
        assert snippet in sent, snippet
