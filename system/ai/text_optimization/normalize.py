"""Phase A: safe, conservative technical cleanup of extracted text.

Only removes/reshapes things that carry no document content of their own:
whitespace runs, empty-line runs, invisible characters, explicit page
numbers, repeated page headers/footers, consecutive OCR duplicate lines,
later exact copies of long paragraphs, symbol-only scan noise, and line
breaks inside words/sentences. Anything that *might* be a real clause or
table cell is kept - when in doubt, a line stays.

Every pass is a single linear walk over the lines (plus dict/set lookups),
so 100k characters normalize in milliseconds.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

PAGE_BREAK = "\f"

# Invisible characters PDF/OCR text commonly carries (zero-width
# space/joiners, word joiner, BOM, soft hyphen) - removed outright; the
# soft hyphen is invisible in the rendered PDF, so dropping it restores the
# word as printed.
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
# Non-breaking/narrow spaces -> plain space.
_SPACES = {0x00A0: " ", 0x2007: " ", 0x202F: " "}

# A horizontal gap of 3+ spaces or any tab is almost always a column gap
# (table/form layout). It is shortened to two spaces, not one, so the
# column boundary stays visible to the model and to the table check below.
_COLUMN_GAP_RE = re.compile(r"\t+| {3,}")
# Dot/underscore leaders (tables of contents, form blanks).
_LEADER_RE = re.compile(r"[._]{4,}")

# Unambiguous page-number lines: "Seite 3", "Seite 3 von 7", "Page 2 of 9",
# "3 von 7", "- 3 -". A bare number is NOT matched here - it may be a table
# cell - see _BARE_NUMBER_RE, only used at page edges when page breaks are
# known.
_EXPLICIT_PAGE_NUMBER_RE = re.compile(
    r"^(?:(?:seite|page|blatt)\s*\d{1,4}(?:\s*(?:von|of|/)\s*\d{1,4})?"
    r"|\d{1,4}\s+(?:von|of)\s+\d{1,4}"
    r"|[-–—]\s*\d{1,4}\s*[-–—])$",
    re.IGNORECASE,
)
_BARE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_PAGE_TOKEN_RE = re.compile(r"\b(?:seite|page|blatt)\b", re.IGNORECASE)
_DIGITS_RE = re.compile(r"\d+")
_WS_RE = re.compile(r"\s+")

# Symbols that make a short line meaningful even without letters/digits
# (section sign, currency, percent, checkbox states).
_MEANINGFUL_SYMBOLS = frozenset("§€$£%☐☑☒✓✔✗✘■□")

_HYPHEN_END_RE = re.compile(r"[A-Za-zÄÖÜäöüß]{2,}-$")
_LIST_MARKER_RE = re.compile(r"^(?:[a-z]|[ivx]{1,4})[).]\s")
_CONJUNCTIONS = frozenset({"und", "oder", "bzw", "sowie", "and", "or"})

# Repeated-line detection without page breaks: a header/footer repeats once
# per page, i.e. a few thousand characters apart. Table rows that happen to
# be identical are adjacent - requiring this minimum spacing between every
# occurrence keeps them.
_MIN_REPEAT_COUNT = 3
_MIN_REPEAT_SPACING_CHARS = 1000
_REPEAT_LINE_MIN_CHARS = 8
_REPEAT_LINE_MAX_CHARS = 120
# Page-edge zone (first/last N non-empty lines of a page) for header/footer
# detection when page breaks are known.
_PAGE_EDGE_LINES = 3
# Consecutive identical lines shorter than this are kept (short table
# cells like "monatlich" legitimately repeat line after line).
_CONSECUTIVE_DUP_MIN_CHARS = 30
# Exact later copies of paragraphs at least this long are dropped - the
# content stays once, so no information is lost (typical OCR double layer).
_DUP_PARAGRAPH_MIN_CHARS = 200
# Line unwrapping only joins lines that are long enough to be running text,
# never short labels/headings.
_UNWRAP_MIN_LINE_CHARS = 30


def is_table_like(line: str) -> bool:
    return "|" in line or "  " in line


def normalize_text(raw: str) -> str:
    """Returns the cleaned text. Never mutates or stores `raw`."""
    text = raw.replace("\r\n", "\n").replace("\r", "\n").translate(_INVISIBLE).translate(_SPACES)

    pages = text.split(PAGE_BREAK)
    has_pages = len(pages) > 1
    page_lines = [[_clean_line(line) for line in page.split("\n")] for page in pages]

    if has_pages:
        page_lines = _drop_page_edge_noise(page_lines)

    lines: list[str] = []
    for index, page in enumerate(page_lines):
        if index:
            lines.append("")
        lines.extend(page)

    lines = [line for line in lines if not _is_noise_line(line)]
    lines = _drop_repeated_lines(lines)
    lines = _drop_consecutive_duplicates(lines)
    paragraphs = _paragraphs(lines)
    paragraphs = [_repair_line_breaks(paragraph) for paragraph in paragraphs]
    paragraphs = _drop_duplicate_paragraphs(paragraphs)
    return "\n\n".join("\n".join(paragraph) for paragraph in paragraphs)


def _clean_line(line: str) -> str:
    line = _LEADER_RE.sub("...", line)
    line = _COLUMN_GAP_RE.sub("  ", line)
    return line.strip()


def _is_noise_line(line: str) -> bool:
    if not line:
        return False
    if _EXPLICIT_PAGE_NUMBER_RE.match(line):
        return True
    if any(ch in _MEANINGFUL_SYMBOLS for ch in line):
        return False
    alnum = sum(ch.isalnum() for ch in line)
    if alnum == 0:
        return True
    # Mostly symbols ("_ _ x _ _", "~~~ ~~ ~") - a real word/value line has
    # far more letters/digits than this.
    return len(line) >= 6 and alnum / len(line) < 0.25


def _edge_key(line: str) -> str:
    # Digits are only ignored for page-number footers ("Seite 2"/"Seite 3");
    # anything else must repeat exactly - "§ 7 ..." and "§ 8 ..." headings
    # or "Anlage 1"/"Anlage 2" at page tops are distinct content.
    key = _WS_RE.sub(" ", line.lower()).strip()
    return _DIGITS_RE.sub("#", key) if _PAGE_TOKEN_RE.search(line) else key


def _drop_page_edge_noise(page_lines: list[list[str]]) -> list[list[str]]:
    """With known page breaks: drop bare page numbers at page edges, and
    lines repeated at the edges of most pages (headers/footers - digits are
    ignored for the comparison so "Seite 2"/"Seite 3" footers match). The
    first occurrence of a repeated header/footer is kept (it may carry e.g.
    the provider's letterhead)."""
    edge_positions: list[set[int]] = []
    pages_per_key: dict[str, int] = defaultdict(int)
    for lines in page_lines:
        non_empty = [i for i, line in enumerate(lines) if line]
        edges = set(non_empty[:_PAGE_EDGE_LINES] + non_empty[-_PAGE_EDGE_LINES:])
        edge_positions.append(edges)
        # Only short lines can be headers/footers - a page with few, long
        # (unwrapped) lines has its body text at the "edges" too.
        for key in {_edge_key(lines[i]) for i in edges if len(lines[i]) <= _REPEAT_LINE_MAX_CHARS}:
            pages_per_key[key] += 1

    min_pages = max(_MIN_REPEAT_COUNT, math.ceil(len(page_lines) * 0.5))
    repeated = {key for key, count in pages_per_key.items() if count >= min_pages}
    seen: set[str] = set()
    result: list[list[str]] = []
    for lines, edges in zip(page_lines, edge_positions):
        kept: list[str] = []
        for i, line in enumerate(lines):
            if i in edges:
                if _BARE_NUMBER_RE.match(line):
                    continue
                key = _edge_key(line)
                if key in repeated and len(line) <= _REPEAT_LINE_MAX_CHARS:
                    if key in seen:
                        continue
                    seen.add(key)
            kept.append(line)
        result.append(kept)
    return result


def _drop_repeated_lines(lines: list[str]) -> list[str]:
    """Without relying on page breaks: drop later copies of a short line
    that recurs >= 3 times with every occurrence well spaced apart
    (header/footer pattern), keeping the first. Footers that only differ by
    a page number ("Vertrag 4711 - Seite 2") are matched digit-insensitively,
    but only when they contain a page token."""
    offsets: dict[str, list[int]] = defaultdict(list)
    position = 0
    keys: list[str | None] = []
    for line in lines:
        key = None
        if _REPEAT_LINE_MIN_CHARS <= len(line) <= _REPEAT_LINE_MAX_CHARS:
            key = _edge_key(line)
            offsets[key].append(position)
        keys.append(key)
        position += len(line) + 1

    repeated = set()
    for key, positions in offsets.items():
        if len(positions) < _MIN_REPEAT_COUNT:
            continue
        gaps = (b - a for a, b in zip(positions, positions[1:]))
        if all(gap >= _MIN_REPEAT_SPACING_CHARS for gap in gaps):
            repeated.add(key)
    if not repeated:
        return lines

    seen: set[str] = set()
    result: list[str] = []
    for line, key in zip(lines, keys):
        if key in repeated:
            if key in seen:
                continue
            seen.add(key)
        result.append(line)
    return result


def _drop_consecutive_duplicates(lines: list[str]) -> list[str]:
    # Table rows are exempt: two identical line items are real content.
    result: list[str] = []
    previous = None
    for line in lines:
        if (
            line
            and line == previous
            and len(line) >= _CONSECUTIVE_DUP_MIN_CHARS
            and not is_table_like(line)
        ):
            continue
        result.append(line)
        if line:
            previous = line
    return result


def _paragraphs(lines: list[str]) -> list[list[str]]:
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if line:
            current.append(line)
        elif current:
            paragraphs.append(current)
            current = []
    if current:
        paragraphs.append(current)
    return paragraphs


def _repair_line_breaks(paragraph: list[str]) -> list[str]:
    """Rejoins words hyphenated across a line break ("Kündigungs-\\nfrist")
    and sentences wrapped mid-sentence. Never touches table-like lines,
    headings/labels (short lines), list items, or a line after a sentence
    end - those breaks carry structure."""
    result: list[str] = []
    for line in paragraph:
        if result:
            previous = result[-1]
            joined = _join(previous, line)
            if joined is not None:
                result[-1] = joined
                continue
        result.append(line)
    return result


def _join(previous: str, line: str) -> str | None:
    if is_table_like(previous) or is_table_like(line) or not line[0].isalpha():
        return None
    if _HYPHEN_END_RE.search(previous):
        if line[0].isupper():
            return previous + line  # "Service-" + "Vertrag" -> keeps the real hyphen
        first_word = line.split(" ", 1)[0].rstrip(".,;:").lower()
        if first_word in _CONJUNCTIONS:
            return previous + " " + line  # "Wartungs- und ..." stays as written
        return previous[:-1] + line
    if (
        line[0].islower()
        and len(previous) >= _UNWRAP_MIN_LINE_CHARS
        and previous[-1] not in ".:;!?"
        and not _LIST_MARKER_RE.match(line)
    ):
        return previous + " " + line
    return None


def _drop_duplicate_paragraphs(paragraphs: list[list[str]]) -> list[list[str]]:
    seen: set[str] = set()
    result: list[list[str]] = []
    for paragraph in paragraphs:
        joined = "\n".join(paragraph)
        if len(joined) >= _DUP_PARAGRAPH_MIN_CHARS:
            if joined in seen:
                continue
            seen.add(joined)
        result.append(paragraph)
    return result
