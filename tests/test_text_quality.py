from __future__ import annotations

from system.text_quality import count_meaningful_characters, is_text_sufficient


def test_count_meaningful_characters_ignores_whitespace_and_control_artifacts():
    assert count_meaningful_characters("\n \t \n") == 0
    assert count_meaningful_characters("3") == 1
    assert count_meaningful_characters("OK") == 2
    assert count_meaningful_characters("Hello, World! 123") == 13


def test_is_text_sufficient_respects_threshold():
    assert is_text_sufficient("x" * 30, minimum_meaningful_characters=30) is True
    assert is_text_sufficient("x" * 29, minimum_meaningful_characters=30) is False
    assert is_text_sufficient("", minimum_meaningful_characters=30) is False
