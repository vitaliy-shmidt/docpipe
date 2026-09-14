from __future__ import annotations

import pytest

from system.assistant.router import DEFAULT_MODE, route

# German + English examples from the task's §52 acceptance list, plus a
# couple of priority-ordering edge cases (§30).
CASES = [
    ("Welche Wartungen sind überfällig?", "maintenance_question"),
    ("Welche Prüfungen stehen an?", "inspection_question"),
    ("Welche Verträge laufen bald aus?", "contract_question"),
    ("Was steht im letzten Wartungsbericht?", "document_question"),
    ("Wie steht mein Hotel technisch da?", "hotel_health_summary"),
    ("Was muss ich wissen?", "general_hotel_question"),
    ("Which maintenance items are overdue?", "maintenance_question"),
    ("Which inspections are due?", "inspection_question"),
    ("Which contracts are expiring soon?", "contract_question"),
    ("What does the latest maintenance report say?", "document_question"),
    ("What's the technical state of my hotel?", "hotel_health_summary"),
    ("What do I need to know?", "general_hotel_question"),
]


@pytest.mark.parametrize("question,expected_mode", CASES)
def test_route_matches_expected_mode(question, expected_mode):
    result = route(question)
    assert result.mode == expected_mode


def test_fallback_has_no_invented_confidence():
    result = route("Was muss ich wissen?")
    assert result.mode == DEFAULT_MODE
    assert result.matched_rule == "fallback"
    assert not hasattr(result, "confidence")


def test_document_intent_wins_over_maintenance_keyword_in_same_question():
    # "Wartungsbericht" contains both a maintenance word ("Wartung") and a
    # document word ("Bericht") - document intent must win (task §30).
    result = route("Was steht im letzten Wartungsbericht?")
    assert result.mode == "document_question"
    assert result.matched_rule == "document_keywords"


def test_routing_is_case_insensitive():
    result = route("WELCHE WARTUNGEN SIND ÜBERFÄLLIG?")
    assert result.mode == "maintenance_question"


def test_every_matched_rule_name_is_distinct_from_fallback_for_real_matches():
    result = route("Welche Verträge laufen bald aus?")
    assert result.matched_rule != "fallback"
