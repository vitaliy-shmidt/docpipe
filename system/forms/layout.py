"""Page layout (text lines, characters, rules and rectangles) on the visible page.

pdfminer.six reports coordinates in its own "device" space. Two passes exist:

- neutral pass (always): a copy with explicit MediaBoxes and /Rotate 0, so
  device space = user space minus the MediaBox origin;
- display pass (only for rotated pages whose text is drawn to be read upright
  on screen - the normal case for e.g. landscape scans stored with /Rotate):
  pdfminer applies /Rotate itself, so lines are grouped in reading direction
  as a viewer shows them. Its coordinates are mapped back to user space
  exactly (inverse of pdfminer's page CTM).

Which pass a page needs is decided from the dominant direction of its glyphs
in the neutral pass: text running at the page's /Rotate angle in user space is
displayed upright. Every rectangle then goes through PageBox.to_visible() (top-
left origin, rotation applied, clipped) and a caller-supplied `PostMap` - the
identity for the template itself, a scale or "no positions" for OCR output
(system/forms/ocr.py). Anything outside the visible page is dropped.
"""

from __future__ import annotations

import io
import math
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

import pikepdf
from pdfminer.high_level import extract_pages
from pdfminer.layout import LAParams, LTChar, LTCurve, LTFigure, LTLine, LTRect, LTTextContainer, LTTextLine

from system.forms.geometry import PageBox, VisibleRect

# visible rect on THIS document's page -> visible rect on the template page (None: no position)
PostMap = Callable[[VisibleRect], VisibleRect | None]
_DeviceMap = Callable[[float, float, float, float], VisibleRect | None]

_RULE_MAX_THICKNESS = 2.0


def identity(rect: VisibleRect) -> VisibleRect | None:
    return rect


@dataclass(frozen=True)
class Char:
    text: str
    rect: VisibleRect


@dataclass
class TextLine:
    text: str
    rect: VisibleRect
    chars: list[Char] = field(default_factory=list)


@dataclass
class PageLayout:
    text: str = ""
    lines: list[TextLine] = field(default_factory=list)
    hrules: list[VisibleRect] = field(default_factory=list)  # horizontal lines (underlines, table rules)
    vrules: list[VisibleRect] = field(default_factory=list)
    rects: list[VisibleRect] = field(default_factory=list)  # closed rectangles thicker than a rule
    display_pass: bool = False

    def blocks(self) -> list[dict]:
        """`text_blocks` of the response: one per line, reading order."""
        blocks = [{"text": ln.text, "position": ln.rect.as_dict()} for ln in self.lines]
        blocks.sort(key=lambda b: (round(b["position"]["y"], 1), b["position"]["x"]))
        return blocks


def _neutral_map(box: PageBox, post: PostMap) -> _DeviceMap:
    mx, my = box.mediabox[0], box.mediabox[1]

    def mapper(x0: float, y0: float, x1: float, y1: float) -> VisibleRect | None:
        visible, _ = box.clip(box.to_visible(x0 + mx, y0 + my, x1 + mx, y1 + my), keep_lines=True)
        return post(visible) if visible is not None else None

    return mapper


def _display_map(box: PageBox, post: PostMap) -> _DeviceMap:
    """Inverse of pdfminer's CTM for /Rotate (pdfminer.pdfinterp.PDFPageInterpreter.process_page)."""
    mx0, my0, mx1, my1 = box.mediabox
    rotation = box.rotation

    def to_user(dx: float, dy: float) -> tuple[float, float]:
        if rotation == 90:
            return mx1 - dy, dx + my0
        if rotation == 180:
            return mx1 - dx, my1 - dy
        if rotation == 270:
            return dy + mx0, my1 - dx
        return dx + mx0, dy + my0

    def mapper(x0: float, y0: float, x1: float, y1: float) -> VisibleRect | None:
        ax, ay = to_user(x0, y0)
        bx, by = to_user(x1, y1)
        visible, _ = box.clip(box.to_visible(min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)),
                              keep_lines=True)
        return post(visible) if visible is not None else None

    return mapper


def _walk(items: Iterable, kind) -> Iterable:
    for item in items:
        if isinstance(item, LTFigure):  # form XObjects
            yield from _walk(item, kind)
        elif isinstance(item, kind):
            yield item


def _chars(layout) -> Iterable[LTChar]:
    for element in layout:
        if isinstance(element, LTTextContainer):
            for line in element:
                if isinstance(line, LTTextLine):
                    yield from (c for c in line if isinstance(c, LTChar))
    yield from _walk(layout, LTChar)


def dominant_angle(layout) -> int | None:
    """Prevailing glyph direction in device space, rounded to 0/90/180/270 (None: no glyphs)."""
    counts: Counter[int] = Counter()
    for char in _chars(layout):
        a, b = char.matrix[0], char.matrix[1]
        if a == 0 and b == 0:
            continue
        counts[int(round(math.degrees(math.atan2(b, a)) / 90.0)) % 4 * 90] += 1
    return counts.most_common(1)[0][0] if counts else None


def _text_line(line, mapper: _DeviceMap) -> TextLine | None:
    text = line.get_text().strip()
    if not text:
        return None
    rect = mapper(*line.bbox)
    if rect is None:
        return None
    chars = []
    for item in line:
        if isinstance(item, LTChar):
            char_rect = mapper(*item.bbox)
            if char_rect is not None:
                chars.append(Char(item.get_text(), char_rect))
    return TextLine(text, rect, chars)


def analyse(layout, mapper: _DeviceMap) -> PageLayout:
    page = PageLayout()
    texts = []
    for element in layout:
        if not isinstance(element, LTTextContainer):
            continue
        box_text = element.get_text().strip()
        if not box_text:
            continue
        texts.append(box_text)
        lines = [ln for ln in element if isinstance(ln, LTTextLine)] or [element]
        for line in lines:
            text_line = _text_line(line, mapper)
            if text_line is not None:
                page.lines.append(text_line)
    page.text = "\n".join(texts)
    for item in _walk(layout, (LTLine, LTRect, LTCurve)):
        if not isinstance(item, (LTLine, LTRect)):
            continue  # free-form paths are not used for detection
        rect = mapper(*item.bbox)
        if rect is None:
            continue
        thin_h, thin_v = rect.height <= _RULE_MAX_THICKNESS, rect.width <= _RULE_MAX_THICKNESS
        if thin_h and not thin_v:
            page.hrules.append(rect)
        elif thin_v and not thin_h:
            page.vrules.append(rect)
        elif isinstance(item, LTRect) and not (thin_h or thin_v):
            page.rects.append(rect)
    return page


def _copy(pdf: pikepdf.Pdf, boxes: list[PageBox], keep_rotation: bool) -> bytes:
    for page, box in zip(pdf.pages, boxes):
        page.obj.MediaBox = pikepdf.Array(list(box.mediabox))
        page.obj.Rotate = box.rotation if keep_rotation else 0
    out = io.BytesIO()
    pdf.save(out, encryption=False)
    return out.getvalue()


def extract(pdf_bytes: bytes, posts: dict[int, PostMap]) -> dict[int, PageLayout]:
    """Analyse the 0-based pages in `posts` of an unencrypted PDF (pages keep their real /Rotate)."""
    wanted = sorted(posts)
    if not wanted:
        return {}
    source = pikepdf.open(io.BytesIO(pdf_bytes))
    try:
        boxes = [PageBox.from_page(page.obj) for page in source.pages]
        neutral = _copy(source, boxes, keep_rotation=False)
        display = _copy(source, boxes, keep_rotation=True)
    finally:
        source.close()

    result: dict[int, PageLayout] = {}
    upright_rotated: list[int] = []
    layouts = extract_pages(io.BytesIO(neutral), page_numbers=set(wanted), laparams=LAParams())
    for index, layout in zip(wanted, layouts):
        box = boxes[index]
        if box.rotation and dominant_angle(layout) == box.rotation:
            upright_rotated.append(index)
            continue
        result[index] = analyse(layout, _neutral_map(box, posts[index]))
    if upright_rotated:
        layouts = extract_pages(io.BytesIO(display), page_numbers=set(upright_rotated), laparams=LAParams())
        for index, layout in zip(upright_rotated, layouts):
            page = analyse(layout, _display_map(boxes[index], posts[index]))
            page.display_pass = True
            result[index] = page
    return result
