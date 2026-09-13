"""Heuristic for deciding whether extracted text is substantive enough to
skip the OCR fallback.

Deliberately simple (V1): count alphanumeric characters, ignoring
whitespace, line breaks, and other control/artifact characters a scanned
PDF's plain-text extraction often still produces (e.g. a lone "\\n" or a
handful of stray bytes). A real text-based PDF easily clears a small
threshold; a scanned one essentially never does. This is a pragmatic
trigger for attempting OCR, not a content classifier - a genuinely short
document (e.g. a page that just says "OK") can still trigger an OCR
attempt. That's an accepted tradeoff, not a bug.
"""

from __future__ import annotations


def count_meaningful_characters(text: str) -> int:
    return sum(1 for ch in text if ch.isalnum())


def is_text_sufficient(text: str, minimum_meaningful_characters: int) -> bool:
    return count_meaningful_characters(text) >= minimum_meaningful_characters
