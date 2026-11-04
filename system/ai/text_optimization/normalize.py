"""Phase A: safe, conservative technical cleanup of extracted text (V2).

Only removes/reshapes things that carry no document content of their own:
whitespace runs, empty-line runs, invisible/control characters, typographic
ligatures, explicit page numbers, repeated page headers/footers, consecutive
OCR duplicate lines/paragraphs, later exact copies of long paragraphs,
symbol-only scan/layout noise, and line breaks inside words/sentences.
Anything that *might* be a real clause, value or table cell is kept - when
in doubt, a line stays.

Never: summarize, rephrase, convert dates/amounts, interpret abbreviations,
translate, reorder. Every output character (apart from collapsed whitespace,
`...` for dot/underscore leaders and a removed hyphenation hyphen) is an
input character in the original order - tests/test_text_optimization.py
checks this as a subsequence invariant.

Every pass is a single linear walk over the lines (plus dict/set lookups),
so 100k characters normalize in well under 100 ms.
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict

PAGE_BREAK = "\f"

# Line separators some extractors emit instead of "\n" (vertical tab, NEL,
# Unicode line/paragraph separator).
_LINE_SEPARATORS_RE = re.compile("\r\n|[\r\v\x85  ]")

# Invisible characters PDF/OCR text commonly carries (zero-width
# space/joiners, word joiner, BOM, soft hyphen) and C0/C1 control characters
# (except tab, newline, form feed) - removed outright. The soft hyphen is
# invisible in the rendered PDF, so dropping it restores the word as printed.
_REMOVED = dict.fromkeys(map(ord, "​‌‍⁠﻿­"), None)
_REMOVED.update(dict.fromkeys((*range(0x00, 0x09), 0x0B, *range(0x0E, 0x20), *range(0x7F, 0xA0)), None))
# Non-breaking/narrow/typographic spaces -> plain space.
_SPACES = dict.fromkeys((0x00A0, 0x1680, *range(0x2000, 0x200B), 0x202F, 0x205F, 0x3000), " ")
# Typographic ligatures (pdf text layers keep them: "Pﬂichten") -> letters,
# so keywords and the tokenizer see the word as printed.
_LIGATURES = {
    0xFB00: "ff", 0xFB01: "fi", 0xFB02: "fl", 0xFB03: "ffi", 0xFB04: "ffl", 0xFB05: "st", 0xFB06: "st"
}
_TRANSLATION = {**_REMOVED, **_SPACES, **_LIGATURES}

# A horizontal gap of 3+ spaces or any tab is almost always a column gap
# (table/form layout). It is shortened to two spaces, not one, so the
# column boundary stays visible to the model and to the table check below.
_COLUMN_GAP_RE = re.compile(r"[ \t]*\t[ \t]*| {3,}")
# Dot/underscore leaders (tables of contents, form blanks), also spaced
# (". . . .").
_LEADER_RE = re.compile(r"[._]{4,}|(?:[.·] ){3,}[.·]")
# Runs of rule characters inside a line ("Preis ========== 450 €").
_RULE_RUN_RE = re.compile(r"([-=*~#•·])\1{3,}")

# Unambiguous page-number lines: "Seite 3", "Seite 3 von 7", "Seite: 3",
# "S. 3 von 7", "Page 2 of 9", "3 von 7", "- 3 -". A bare number ("24") or
# fraction ("3/7") is NOT matched here - it may be a table cell or value -
# see _edge_page_numbers, only used at page edges when page breaks are known.
_EXPLICIT_PAGE_NUMBER_RE = re.compile(
    r"^(?:(?:seite|page|blatt|pg\.?)\s*:?\s*\d{1,4}(?:\s*(?:von|of|/)\s*\d{1,4})?"
    r"|(?:s\.|p\.)\s*\d{1,4}\s*(?:von|of|/)\s*\d{1,4}"
    r"|\d{1,4}\s+(?:von|of)\s+\d{1,4}"
    r"|[-–—]\s*\d{1,4}\s*[-–—])$",
    re.IGNORECASE,
)
_EDGE_PAGE_NUMBER_RE = re.compile(r"^(\d{1,4})(?:\s*/\s*(\d{1,4}))?$")
_PAGE_TOKEN_RE = re.compile(r"\b(?:seite|page|blatt)\b", re.IGNORECASE)
_PAGE_COUNTER_END_RE = re.compile(r"\b\d{1,4}\s*/\s*\d{1,4}$")
_DIGITS_RE = re.compile(r"\d+")
_URL_OR_MAIL_RE = re.compile(r"(?:www\.|https?:|\S+@)", re.IGNORECASE)
# A table-of-contents entry: numbered heading, dot leader, page number
# ("§ 4 Laufzeit ....... 5", "3.2 Preise ... 7"). Only removed as a run, see
# _drop_table_of_contents.
_TOC_ENTRY_RE = re.compile(
    r"^(?P<title>(?:§\s*\d+[a-z]?|art(?:ikel|icle)?\.?\s*\d+|anlage\s+\d+|annex\s+\d+|abschnitt\s+\d+"
    r"|\d{1,2}(?:\.\d{1,2})*\.?|[IVX]{1,5}\.)\s+\S.{0,100}?)\s*\.\.\.\s*(?P<page>\d{1,4})$",
    re.IGNORECASE,
)
_TOC_MIN_ENTRIES = 3
# Share of a TOC run's titles that must reappear as a line of their own
# elsewhere (the actual section headings) - a numbered value list does not.
_TOC_MIN_TITLE_MATCH_RATIO = 2 / 3
_WS_RE = re.compile(r"\s+")

# Symbols that make a short line meaningful even without letters/digits
# (section sign, currency, percent, checkbox states).
_MEANINGFUL_SYMBOLS = frozenset("§€$£%☐☑☒✓✔✗✘■□")

# Short business values that legitimately recur through a document (table
# cells, repeated terms) - never treated as a repeated header/footer:
# amounts, percentages, dates, durations, section references, intervals.
_VALUE_RE = re.compile(
    r"\d[\d.,']*\s?(?:€|eur\b|euro\b|chf\b|usd\b|\$|%)"
    r"|(?:€|eur\b|chf\b|usd\b|\$)\s?\d"
    r"|\b\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})\b|\b\d{4}-\d{2}-\d{2}\b"
    r"|\b\d+\s*(?:tag|tage|tagen|woche|wochen|monat|monate|monaten|jahr|jahre|jahren"
    r"|days?|weeks?|months?|years?)\b"
    r"|§\s*\d",
    re.IGNORECASE,
)
_INTERVAL_WORDS = frozenset(
    {
        "täglich",
        "wöchentlich",
        "monatlich",
        "quartalsweise",
        "vierteljährlich",
        "halbjährlich",
        "jährlich",
        "einmalig",
        "daily",
        "weekly",
        "monthly",
        "quarterly",
        "annually",
        "yearly",
    }
)

_LETTERS = "A-Za-zÄÖÜäöüß"
# Line-end hyphen (ASCII, Unicode hyphen, non-breaking hyphen) after a word.
_HYPHEN_END_RE = re.compile(rf"(?<![{_LETTERS}])([{_LETTERS}]{{2,}})[-‐‑]$")
_LIST_MARKER_RE = re.compile(r"^(?:[a-z]|[ivx]{1,4})[).]\s")
_HEADING_START_RE = re.compile(
    r"^(?:§\s*\d|art(?:ikel|icle)?\.?\s*\d|\d{1,2}(?:\.\d{1,2})*\.?\s+\S|[IVX]{1,5}\.\s)", re.I
)
# "Vertragsnummer: WV-1", "Contact person: ..." - a form field label.
_FIELD_LABEL_RE = re.compile(r"^[^\s:]{1,30}(?: [^\s:]{1,30}){0,3}:(?:\s|$)")
_LOWER_WORD_RE = re.compile(rf"[a-zäöüß][{_LETTERS}]*")
# Capitalized words that never end a line of running text, and capitalized
# units that follow a number ("Laufzeit von 24" + "Monaten").
_ARTICLES = frozenset(
    {"Der", "Die", "Das", "Den", "Dem", "Des", "Ein", "Eine", "Einer", "Eines", "Einem", "Einen", "The"}
)
_NUMBER_RE = re.compile(r"\d[\d.,]*")
_UNIT_WORDS = frozenset(
    {
        "Tag", "Tage", "Tagen", "Werktage", "Werktagen", "Kalendertage", "Kalendertagen",
        "Woche", "Wochen", "Monat", "Monate", "Monaten", "Jahr", "Jahre", "Jahren",
        "Stunden", "Euro", "EUR", "Prozent",
    }
)  # fmt: skip
# Words after "Wartungs-" that mean a suspended compound, not a split word.
_CONJUNCTIONS = frozenset(
    {"und", "oder", "bzw", "beziehungsweise", "sowie", "bis", "als", "wie", "and", "or", "nor"}
)

# Repeated-line detection without page breaks: a header/footer repeats once
# per page, i.e. a few thousand characters apart. Table rows that happen to
# be identical are adjacent - requiring this minimum spacing between every
# occurrence keeps them.
_MIN_REPEAT_COUNT = 3
_MIN_REPEAT_SPACING_CHARS = 1000
_REPEAT_LINE_MIN_CHARS = 8
_REPEAT_LINE_MAX_CHARS = 120
# Page-edge zone (first/last N content lines of a page) for header/footer
# detection when page breaks are known. Page-number and rule lines are not
# counted, so a 3-line letterhead plus page counter still fits.
_PAGE_EDGE_LINES = 3
# Consecutive identical lines shorter than this are kept (short table
# cells like "monatlich" legitimately repeat line after line).
_CONSECUTIVE_DUP_MIN_CHARS = 30
# Directly repeated paragraphs at least this long are OCR double layers.
_CONSECUTIVE_DUP_PARAGRAPH_MIN_CHARS = 40
# Exact later copies of paragraphs at least this long are dropped - the
# content stays once, so no information is lost (typical OCR double layer).
_DUP_PARAGRAPH_MIN_CHARS = 200
# Line unwrapping only joins lines that are long enough to be running text,
# never short labels/headings.
_UNWRAP_MIN_LINE_CHARS = 30
# A continuation starting with a capital letter (German nouns) is only
# joined after a physically full-width line, and only if the continuation is
# itself full-width or ends the sentence - this keeps headings, addresses,
# unmarked list items and other deliberately short lines on their own.
_UNWRAP_UPPER_MIN_PREVIOUS_CHARS = 55
_UNWRAP_UPPER_MIN_LINE_CHARS = 55

# Placeholder for a removed rule/symbol line: it is dropped at the end, but
# until then it prevents the lines above and below from being joined (a
# horizontal rule in a table must not merge two rows into one).
_BARRIER = "\x00"


def is_table_like(line: str) -> bool:
    return "|" in line or "  " in line


def normalize_text(raw: str) -> str:
    """Returns the cleaned text. Never mutates or stores `raw`."""
    text = _LINE_SEPARATORS_RE.sub("\n", raw).translate(_TRANSLATION)

    pages = text.split(PAGE_BREAK)
    has_pages = len(pages) > 1
    page_lines = [_clean_page(page) for page in pages]

    repeated_edges: set[str] = set()
    if has_pages:
        page_lines, repeated_edges = _drop_page_edge_noise(page_lines)

    lines = _join_pages(page_lines, repeated_edges)
    lines = _drop_table_of_contents(lines)
    lines = _drop_repeated_lines(lines)
    lines = _drop_consecutive_duplicates(lines)
    paragraphs = _paragraphs(lines)
    paragraphs = [_repair_line_breaks(paragraph) for paragraph in paragraphs]
    paragraphs = _drop_duplicate_paragraphs([p for p in paragraphs if p])
    return "\n\n".join("\n".join(paragraph) for paragraph in paragraphs)


def _clean_page(page: str) -> list[str]:
    lines = []
    for line in page.split("\n"):
        line = _clean_line(line)
        if _EXPLICIT_PAGE_NUMBER_RE.match(line):
            continue
        lines.append(_BARRIER if _is_symbol_noise(line) else line)
    return lines


def _clean_line(line: str) -> str:
    line = _LEADER_RE.sub("...", line)
    line = _RULE_RUN_RE.sub(r"\1\1\1", line)
    line = _COLUMN_GAP_RE.sub("  ", line)
    return line.strip()


def _is_symbol_noise(line: str) -> bool:
    if not line or not _MEANINGFUL_SYMBOLS.isdisjoint(line):
        return False
    alnum = sum(map(str.isalnum, line))
    if alnum == 0:
        return True
    # Mostly symbols ("_ _ x _ _", "~~~ ~~ ~") - a real word/value line has
    # far more letters/digits than this.
    return len(line) >= 6 and alnum / len(line) < 0.25


def _is_content(line: str) -> bool:
    return bool(line) and line != _BARRIER


def _is_protected_value(line: str) -> bool:
    """Short value lines (amounts, dates, durations, § refs, intervals) are
    content even when they recur - unless the line is a page footer."""
    if _PAGE_TOKEN_RE.search(line):
        return False
    return bool(_VALUE_RE.search(line)) or line.lower().rstrip(".,;") in _INTERVAL_WORDS


def _edge_key(line: str, *, page_counter: bool = False) -> str:
    # Digits are only ignored for page-number footers ("Seite 2"/"Seite 3",
    # at page edges also "Vertrag 4711 | 2/9"); anything else must repeat
    # exactly - "§ 7 ..." and "§ 8 ..." headings or "Anlage 1"/"Anlage 2" at
    # page tops are distinct content.
    key = _WS_RE.sub(" ", line.lower()).strip()
    if _PAGE_TOKEN_RE.search(line) or (page_counter and _PAGE_COUNTER_END_RE.search(key)):
        return _DIGITS_RE.sub("#", key)
    return key


def _drop_page_edge_noise(page_lines: list[list[str]]) -> tuple[list[list[str]], set[str]]:
    """With known page breaks: drop page numbers at page edges, and lines
    repeated at the edges of most pages (headers/footers - digits are
    ignored for page-counter lines so "Seite 2"/"Seite 3" footers match).
    The first occurrence of a repeated header/footer is kept (it may carry
    e.g. the provider's letterhead or the contract number). Returns the
    pages and the repeated keys."""
    edge_positions: list[set[int]] = []
    pages_per_key: dict[str, int] = defaultdict(int)
    for lines in page_lines:
        content = [i for i, line in enumerate(lines) if _is_content(line)]
        edges = set(content[:_PAGE_EDGE_LINES] + content[-_PAGE_EDGE_LINES:])
        edge_positions.append(edges)
        # Only short lines can be headers/footers - a page with few, long
        # (unwrapped) lines has its body text at the "edges" too.
        keys = {
            _edge_key(lines[i], page_counter=True)
            for i in edges
            if len(lines[i]) <= _REPEAT_LINE_MAX_CHARS and not _is_protected_value(lines[i])
        }
        for key in keys:
            pages_per_key[key] += 1

    min_pages = max(_MIN_REPEAT_COUNT, math.ceil(len(page_lines) * 0.5))
    repeated = {key for key, count in pages_per_key.items() if count >= min_pages}
    page_numbers = _edge_page_numbers(page_lines, edge_positions)
    seen: set[str] = set()
    result: list[list[str]] = []
    for page_index, (lines, edges) in enumerate(zip(page_lines, edge_positions)):
        kept: list[str] = []
        for i, line in enumerate(lines):
            if i in edges:
                if (page_index, i) in page_numbers:
                    continue
                key = _edge_key(line, page_counter=True)
                if key in repeated and len(line) <= _REPEAT_LINE_MAX_CHARS and not _is_protected_value(line):
                    if key in seen:
                        continue
                    seen.add(key)
            kept.append(line)
        result.append(kept)
    return result, repeated


def _edge_page_numbers(page_lines: list[list[str]], edge_positions: list[set[int]]) -> set[tuple[int, int]]:
    """Bare numbers ("4") and counters ("4/17") at page edges that form the
    page sequence - number minus page index constant (and the same total)
    on at least half the pages. A lone "24" or "4711" at a page edge that
    does not follow the sequence is a value and stays."""
    candidates: list[tuple[int, int, tuple[int, str | None]]] = []
    for page_index, (lines, edges) in enumerate(zip(page_lines, edge_positions)):
        for i in edges:
            match = _EDGE_PAGE_NUMBER_RE.match(lines[i])
            if match:
                candidates.append((page_index, i, (int(match.group(1)) - page_index, match.group(2))))
    if not candidates:
        return set()
    min_pages = max(2, math.ceil(len(page_lines) * 0.5))
    counts = Counter(pattern for _, _, pattern in candidates)
    return {(page, i) for page, i, pattern in candidates if counts[pattern] >= min_pages}


def _join_pages(page_lines: list[list[str]], repeated_edges: set[str]) -> list[str]:
    """Pages are separated by a paragraph break - except when a sentence
    clearly runs on across the page break (same rules as inside a page),
    and neither side is a kept header/footer line."""
    lines: list[str] = []
    for page in page_lines:
        if lines:
            last = _edge_content(reversed(lines))
            first = _edge_content(page)
            if (
                last is not None
                and first is not None
                and _edge_key(last, page_counter=True) not in repeated_edges
                and _edge_key(first, page_counter=True) not in repeated_edges
                and _join(last, first, len(last)) is not None
            ):
                while lines and not lines[-1]:
                    lines.pop()
                page = list(page)
                while page and not page[0]:
                    page.pop(0)
            else:
                lines.append("")
        lines.extend(page)
    return lines


def _edge_content(lines) -> str | None:
    """First non-empty line of `lines` (pass reversed() for the last one);
    None if that is a removed rule line or there is none."""
    for line in lines:
        if line:
            return line if line != _BARRIER else None
    return None


def _drop_repeated_lines(lines: list[str]) -> list[str]:
    """Without relying on page breaks: drop later copies of a short line
    that recurs >= 3 times with every occurrence well spaced apart
    (header/footer pattern), keeping the first. Footers that only differ by
    a page number ("Vertrag 4711 - Seite 2") are matched digit-insensitively,
    but only when they contain a page token. Short business values (amounts,
    dates, durations, intervals) are never treated as headers."""
    offsets: dict[str, list[int]] = defaultdict(list)
    position = 0
    keys: list[str | None] = []
    for line in lines:
        key = None
        if (
            _REPEAT_LINE_MIN_CHARS <= len(line) <= _REPEAT_LINE_MAX_CHARS
            and _may_be_header(line)
            and not _is_protected_value(line)
        ):
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


def _may_be_header(line: str) -> bool:
    """Without page positions, a recurring sentence or the wrapped middle
    of one ("für leichte Fahrlässigkeit ist ausgeschlossen, soweit ...")
    must not pass for a header: header/footer lines neither end with
    sentence punctuation nor start lowercase (URLs/e-mail excepted)."""
    if _PAGE_TOKEN_RE.search(line):
        return True
    if line[-1] in ".!?;:,":
        return False
    return not line[0].islower() or bool(_URL_OR_MAIL_RE.match(line))


def _drop_consecutive_duplicates(lines: list[str]) -> list[str]:
    # Table rows are exempt: two identical line items are real content -
    # decided on the kept first copy. Compared whitespace-insensitively (an
    # OCR double layer may differ in spacing only); the first copy is kept
    # exactly as it was.
    result: list[str] = []
    previous_key = None
    previous_is_table = False
    for line in lines:
        if not line:
            result.append(line)
            continue
        key = _WS_RE.sub(" ", line)
        if key == previous_key and len(key) >= _CONSECUTIVE_DUP_MIN_CHARS and not previous_is_table:
            continue
        result.append(line)
        previous_key = key
        previous_is_table = is_table_like(line)
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


def _drop_table_of_contents(lines: list[str]) -> list[str]:
    """Drops runs of >= 3 table-of-contents entries (blank lines between
    allowed) whose page numbers never decrease and whose titles (at least
    two thirds) reappear as lines of their own - the actual section
    headings. In the TOC they only repeat every clause keyword and would
    crowd the document head. A leader list of values ("Laufzeit ... 24",
    "1. Kündigungsfrist ... 3") is not repeated as headings, so it stays."""
    runs: list[list[tuple[int, str]]] = []
    run: list[tuple[int, str]] = []
    last_page = -1

    def close() -> None:
        if len(run) >= _TOC_MIN_ENTRIES:
            runs.append(list(run))
        run.clear()

    for index, line in enumerate(lines):
        if not line:
            continue
        match = _TOC_ENTRY_RE.match(line)
        if match is None:
            close()
            continue
        page = int(match.group("page"))
        if run and page < last_page:
            close()
        run.append((index, _toc_key(match.group("title"))))
        last_page = page
    close()
    if not runs:
        return lines

    candidates = {index for run in runs for index, _ in run}
    headings = {_toc_key(line) for index, line in enumerate(lines) if line and index not in candidates}
    drop: set[int] = set()
    for run in runs:
        found = sum(1 for _, title in run if title in headings)
        if found >= len(run) * _TOC_MIN_TITLE_MATCH_RATIO:
            drop.update(index for index, _ in run)
    if not drop:
        return lines
    return [line for index, line in enumerate(lines) if index not in drop]


def _toc_key(text: str) -> str:
    return _WS_RE.sub(" ", text).strip().lower()


def _repair_line_breaks(paragraph: list[str]) -> list[str]:
    """Rejoins words hyphenated across a line break ("Kündigungs-\\nfrist")
    and sentences wrapped mid-sentence. Never touches table-like lines,
    headings/labels (short lines), list items, form fields, a line after a
    sentence end, or anything across a removed rule line - those breaks
    carry structure. Removes the rule-line placeholders.

    Decisions only look at the last physical line, and a logical line is
    collected as parts, so long paragraphs stay linear."""
    result: list[str] = []
    parts: list[str] = []
    logical_len = 0
    barrier = False
    for line in paragraph:
        if line == _BARRIER:
            barrier = True
            continue
        if parts and not barrier:
            separator = _join(parts[-1], line, logical_len)
            if separator is not None:
                if separator == _DEHYPHENATE:
                    parts[-1] = parts[-1][:-1]
                else:
                    parts.append(separator)
                parts.append(line)
                logical_len += len(line) + len(separator)
                continue
        if parts:
            result.append("".join(parts))
        parts = [line]
        logical_len = len(line)
        barrier = False
    if parts:
        result.append("".join(parts))
    return result


# _join result: drop the hyphen at the end of the previous line.
_DEHYPHENATE = "-"


def _join(previous: str, line: str, logical_len: int) -> str | None:
    """How to join physical line `line` to the line before it: " " (wrapped
    sentence), "" (hyphen kept: "Service-" + "Vertrag"), _DEHYPHENATE
    ("Kündigungs-" + "frist") or None (keep the line break). `previous` is
    the previous physical line, `logical_len` the length of the logical
    line it ends."""
    if is_table_like(previous) or is_table_like(line) or not line[0].isalpha():
        return None
    hyphen = _HYPHEN_END_RE.search(previous)
    if hyphen:
        if line[0].isupper() or hyphen.group(1).isupper():
            return ""  # "Service-" + "Vertrag", "EU-" + "weit" -> keeps the real hyphen
        first_word = line.split(" ", 1)[0].rstrip(".,;:").lower()
        if first_word in _CONJUNCTIONS:
            return " "  # "Wartungs- und ..." stays as written
        return _DEHYPHENATE
    if previous[-1] in ".:;!?" or _LIST_MARKER_RE.match(line):
        return None
    if line[0].islower():
        return " " if logical_len >= _UNWRAP_MIN_LINE_CHARS else None
    # Capitalized continuation (German nouns, names): only inside clearly
    # running text - the previous physical line is full-width and ends with
    # a lowercase word ("... die regelmäßige Wartung der" + "Aufzugsanlagen
    # ..."), the line is not a heading, label or all-caps title, and it is
    # either full-width itself or ends the sentence.
    if (
        len(previous) >= _UNWRAP_UPPER_MIN_PREVIOUS_CHARS
        and _ends_mid_sentence(previous.rsplit(" ", 1)[-1], line)
        and not _HEADING_START_RE.match(previous)
        and not _HEADING_START_RE.match(line)
        and not _FIELD_LABEL_RE.match(line)
        and not line.isupper()
        and (len(line) >= _UNWRAP_UPPER_MIN_LINE_CHARS or line[-1] in ".!?;")
    ):
        return " "
    return None


def _ends_mid_sentence(last_word: str, line: str) -> bool:
    """The previous line's last word cannot end a German/English line of
    running text: a lowercase word, an article ("Der" + "Auftragnehmer"),
    or a number directly followed by its unit ("24" + "Monaten")."""
    if _LOWER_WORD_RE.fullmatch(last_word) or last_word in _ARTICLES:
        return True
    return bool(_NUMBER_RE.fullmatch(last_word)) and line.split(" ", 1)[0].rstrip(".,;") in _UNIT_WORDS


def _drop_duplicate_paragraphs(paragraphs: list[list[str]]) -> list[list[str]]:
    """Drops later exact copies of long paragraphs anywhere, and a directly
    repeated medium paragraph (OCR double layer). Compared
    whitespace-insensitively; table paragraphs only by the long rule (two
    identical adjacent tables would be rare, two identical rows are not)."""
    seen: set[str] = set()
    result: list[list[str]] = []
    previous_key = None
    for paragraph in paragraphs:
        key = _WS_RE.sub(" ", " ".join(paragraph))
        if len(key) >= _DUP_PARAGRAPH_MIN_CHARS:
            if key in seen:
                continue
            seen.add(key)
        elif (
            key == previous_key
            and len(key) >= _CONSECUTIVE_DUP_PARAGRAPH_MIN_CHARS
            # Prose only: short value lines ("31.12.2027", "12 Monate") or
            # table rows may legitimately repeat paragraph after paragraph.
            and all(len(line) >= _CONSECUTIVE_DUP_MIN_CHARS and not is_table_like(line) for line in paragraph)
        ):
            continue
        result.append(paragraph)
        previous_key = key
    return result
