"""Proposals for input fields on static pages (Phase 2.1b).

Deterministic layout heuristics - no AI, no image processing. Every result is a
PROPOSAL (`source: "detected"`, `confidence` 0..1) that PDF4Fill shows for
confirmation; nothing is adopted automatically. A name is only ever taken from
neighbouring text on the page (`label`), never invented; without a label the
proposal has `name: null`.

Works in the visible-page system (top-left origin, y downwards, rotation and
CropBox already applied - see system/forms/layout.py), so "left of" and
"above" mean what a reader sees, on rotated pages too.

Detectors:
- underline: a horizontal rule with empty space above it (`Name: ________`,
  or a caption below it as under a signature line);
- box: an empty rectangle of input size (single- or multi-line);
- checkbox: a small empty square, label normally to its right;
- placeholder: a run of `_` or `.` characters in the text (also found in OCR
  text, the only detector that works on scanned pages).

Not detected (see docs/forms-inspect.md "Detection limits"): lines inside
scanned images, grids drawn from separate line segments (their cells), comb
fields, circles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from system.forms.geometry import VisibleRect
from system.forms.layout import PageLayout, TextLine

MAX_PER_PAGE = 200
LOW_CONFIDENCE = 0.6

_UNDERLINE_MIN_LENGTH = 30.0
_UNDERLINE_FIELD_HEIGHT = 16.0
_UNDERLINE_MIN_HEIGHT = 8.0
_BOX_MIN_WIDTH, _BOX_MIN_HEIGHT, _BOX_MAX_HEIGHT = 30.0, 10.0, 200.0
_CHECKBOX_MIN, _CHECKBOX_MAX = 6.0, 20.0
_LABEL_MAX_GAP = 60.0  # label right edge -> field start (same row)
_LABEL_ABOVE_GAP = 16.0
_LABEL_BELOW_GAP = 14.0
_CHECKBOX_LABEL_GAP = 30.0
_RULE_JOIN = 2.0  # a rule ending on a vertical rule belongs to a box/grid
_PLACEHOLDER = re.compile(r"_{4,}|\.{6,}|…{2,}")
_LABEL_STRIP = " \t:*_.…"


@dataclass(frozen=True)
class Candidate:
    page: int
    rect: VisibleRect
    type: str  # text | checkbox
    detection: str  # underline | box | checkbox | placeholder
    confidence: float
    label: TextLine | None
    label_text: str | None
    multiline: bool = False


def _overlap(a: VisibleRect, b: VisibleRect) -> float:
    w = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    h = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    return w * h if w > 0 and h > 0 else 0.0


def _area(r: VisibleRect) -> float:
    return max(r.width, 0.0) * max(r.height, 0.0)


def _overlaps_much(a: VisibleRect, b: VisibleRect, share: float = 0.3) -> bool:
    inter = _overlap(a, b)
    smaller = min(_area(a), _area(b))
    return smaller > 0 and inter / smaller > share


def _vertical_overlap(a: VisibleRect, b: VisibleRect) -> float:
    return max(0.0, min(a.y + a.height, b.y + b.height) - max(a.y, b.y))


def _has_text_inside(area: VisibleRect, layout: PageLayout, margin: float = 1.0) -> bool:
    inner = VisibleRect(area.x + margin, area.y + margin, area.width - 2 * margin, area.height - 2 * margin)
    if inner.width <= 0 or inner.height <= 0:
        return False
    for line in layout.lines:
        for char in line.chars or []:
            if char.text.strip() and _overlap(char.rect, inner) > 0.3 * _area(char.rect):
                return True
    return False


def _clean_label(text: str) -> str | None:
    cleaned = _PLACEHOLDER.sub(" ", text).strip(_LABEL_STRIP).strip()
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned or None


def _label_left(area: VisibleRect, layout: PageLayout, max_gap: float = _LABEL_MAX_GAP) -> TextLine | None:
    best, best_gap = None, None
    for line in layout.lines:
        right = line.rect.x + line.rect.width
        gap = area.x - right
        same_row = _vertical_overlap(line.rect, area) >= 0.4 * min(line.rect.height, area.height)
        if -2.0 <= gap <= max_gap and same_row:
            if best_gap is None or gap < best_gap:
                best, best_gap = line, gap
    return best


def _label_right(area: VisibleRect, layout: PageLayout) -> TextLine | None:
    best, best_gap = None, None
    for line in layout.lines:
        gap = line.rect.x - (area.x + area.width)
        if -1.0 <= gap <= _CHECKBOX_LABEL_GAP and _vertical_overlap(line.rect, area) >= 0.4 * min(
                line.rect.height, area.height):
            if best_gap is None or gap < best_gap:
                best, best_gap = line, gap
    return best


def _label_above(area: VisibleRect, layout: PageLayout) -> TextLine | None:
    best, best_gap = None, None
    for line in layout.lines:
        gap = area.y - (line.rect.y + line.rect.height)
        starts_near = (abs(line.rect.x - area.x) <= 12.0
                       or area.x <= line.rect.x <= area.x + area.width * 0.5)
        if -1.0 <= gap <= _LABEL_ABOVE_GAP and starts_near:
            if best_gap is None or gap < best_gap:
                best, best_gap = line, gap
    return best


def _label_below(rule: VisibleRect, layout: PageLayout) -> TextLine | None:
    best, best_gap = None, None
    for line in layout.lines:
        gap = line.rect.y - (rule.y + rule.height)
        horizontal = _overlap(VisibleRect(line.rect.x, 0, line.rect.width, 1),
                              VisibleRect(rule.x, 0, rule.width, 1))
        if -1.0 <= gap <= _LABEL_BELOW_GAP and horizontal >= 0.5 * line.rect.width:
            if best_gap is None or gap < best_gap:
                best, best_gap = line, gap
    return best


def _score(base: float, label: TextLine | None, label_text: str | None) -> float:
    score = base
    if label is not None and label_text:
        score += 0.3
        if label.text.rstrip().endswith(":"):
            score += 0.1
    return round(min(score, 0.95), 2)


def _joins_vertical_rules(rule: VisibleRect, layout: PageLayout) -> bool:
    """True when both ends of a horizontal rule meet vertical rules (box or table drawn from lines)."""
    def meets(x: float) -> bool:
        return any(
            abs(v.x + v.width / 2 - x) <= _RULE_JOIN
            and v.y - _RULE_JOIN <= rule.y <= v.y + v.height + _RULE_JOIN
            for v in layout.vrules
        )
    return meets(rule.x) and meets(rule.x + rule.width)


def _space_above(rule: VisibleRect, layout: PageLayout) -> float:
    """Free height above a rule up to the nearest rule or text line spanning its middle third."""
    limit = _UNDERLINE_FIELD_HEIGHT
    mid_x0, mid_x1 = rule.x + rule.width / 3, rule.x + 2 * rule.width / 3
    for other in layout.hrules:
        if other is rule or other.y >= rule.y:
            continue
        if other.x < mid_x1 and other.x + other.width > mid_x0:
            limit = min(limit, rule.y - (other.y + other.height))
    return limit


def _underlines(page: int, layout: PageLayout, page_width: float) -> list[Candidate]:
    found = []
    for rule in layout.hrules:
        if rule.width < _UNDERLINE_MIN_LENGTH or _joins_vertical_rules(rule, layout):
            continue
        height = _space_above(rule, layout)
        if height < _UNDERLINE_MIN_HEIGHT:
            continue
        area = VisibleRect(rule.x, rule.y - height, rule.width, height)
        if _has_text_inside(area, layout):
            continue  # an underlined heading or text, not an empty line
        label = _label_left(area, layout)
        if label is None:
            label = _label_below(rule, layout)
        if label is None and rule.width > 0.85 * page_width:
            continue  # page-wide separator without any caption
        text = _clean_label(label.text) if label else None
        found.append(Candidate(page, area, "text", "underline", _score(0.45, label, text), label, text))
    return found


def _boxes(page: int, layout: PageLayout, page_width: float, page_height: float) -> list[Candidate]:
    found = []
    for rect in layout.rects:
        if _CHECKBOX_MIN <= rect.width <= _CHECKBOX_MAX and _CHECKBOX_MIN <= rect.height <= _CHECKBOX_MAX:
            if not 0.75 <= rect.width / rect.height <= 1.33 or _has_text_inside(rect, layout, margin=0.5):
                continue
            label = _label_right(rect, layout) or _label_left(rect, layout, max_gap=20.0)
            text = _clean_label(label.text) if label else None
            found.append(Candidate(page, rect, "checkbox", "checkbox", _score(0.5, label, text), label, text))
            continue
        if rect.width < _BOX_MIN_WIDTH or not _BOX_MIN_HEIGHT <= rect.height <= _BOX_MAX_HEIGHT:
            continue
        if rect.width > 0.8 * page_width and rect.height > 0.5 * page_height:
            continue  # page frame
        if any(o is not rect and _overlap(o, rect) >= 0.9 * _area(o) and _area(o) < _area(rect)
               for o in layout.rects):
            continue  # a container around other boxes, e.g. a section frame
        if _has_text_inside(rect, layout):
            continue
        label = _label_left(rect, layout) or _label_above(rect, layout)
        text = _clean_label(label.text) if label else None
        found.append(Candidate(page, rect, "text", "box", _score(0.5, label, text), label, text,
                               multiline=rect.height > 30.0))
    return found


def _placeholders(page: int, layout: PageLayout) -> list[Candidate]:
    found = []
    for line in layout.lines:
        if not line.chars:
            continue
        joined = "".join(c.text for c in line.chars)
        for match in _PLACEHOLDER.finditer(joined):
            run = line.chars[match.start():match.end()]
            x0 = min(c.rect.x for c in run)
            x1 = max(c.rect.x + c.rect.width for c in run)
            area = VisibleRect(x0, line.rect.y, x1 - x0, line.rect.height)
            before = _clean_label(joined[:match.start()].split("  ")[-1]) if match.start() else None
            label = line if before else _label_left(area, layout)
            text = before or (_clean_label(label.text) if label else None)
            found.append(Candidate(page, area, "text", "placeholder", _score(0.55, label, text), label, text))
    return found


def detect(page: int, layout: PageLayout, page_width: float, page_height: float,
           native: list[VisibleRect]) -> tuple[list[Candidate], bool]:
    """Proposals for one page, best first per area; (candidates, truncated)."""
    raw = (_placeholders(page, layout) + _boxes(page, layout, page_width, page_height)
           + _underlines(page, layout, page_width))
    raw = [c for c in raw if not any(_overlaps_much(c.rect, n) for n in native)]
    raw.sort(key=lambda c: -c.confidence)
    kept: list[Candidate] = []
    for candidate in raw:
        if not any(_overlaps_much(candidate.rect, k.rect, 0.5) for k in kept):
            kept.append(candidate)
    truncated = len(kept) > MAX_PER_PAGE
    kept = kept[:MAX_PER_PAGE]
    kept.sort(key=lambda c: (round(c.rect.y, 1), round(c.rect.x, 1)))
    return kept, truncated
