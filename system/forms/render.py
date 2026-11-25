"""The single rendering path of forms/fill (Phase 2.2) - used for preview and final output.

Everything is drawn with reportlab onto one overlay page per template page, in
DISPLAY coordinates: the visible page as a viewer shows it (CropBox, rotation
applied), origin bottom-left, size = the page's displayed width/height. The
contract's top-left positions convert as  y_bottom = height - y - h.
system/forms/fill.py then places that overlay page onto the template page with
the matrix for its CropBox and /Rotate.

Text layout is deterministic and never truncates: a value that does not fit -
after auto-fit down to `min_font_size` if enabled - raises TextOverflow.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from system.forms import fonts
from system.forms.geometry import VisibleRect

PAD_X = 2.0  # inner horizontal padding of a text box
PAD_Y = 1.0
LINE_SPACING = 1.15  # line height = font size * LINE_SPACING
AUTO_FIT_STEP = 0.5


class TextOverflow(Exception):
    """The text does not fit into its box at any permitted size."""


@dataclass(frozen=True)
class TextStyle:
    family: str = "sans"
    size: float = 10.0
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    weight: str = "normal"
    style: str = "normal"
    align: str = "left"
    auto_fit: bool = False
    min_size: float = 6.0

    @property
    def font(self) -> str:
        return fonts.font_name(self.family, self.weight, self.style)


@dataclass(frozen=True)
class TextLayout:
    font: str
    size: float
    lines: tuple[str, ...]
    multiline: bool


def _wrap(text: str, font: str, size: float, max_width: float) -> list[str] | None:
    """Greedy word wrap; words longer than a line are broken between characters. None: cannot fit."""
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words = paragraph.split(" ")
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if fonts.width(candidate, font, size) <= max_width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            while fonts.width(word, font, size) > max_width:
                cut = len(word)
                while cut > 1 and fonts.width(word[:cut], font, size) > max_width:
                    cut -= 1
                if fonts.width(word[:cut], font, size) > max_width:
                    return None  # not even one character fits
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


def _fits(
    text: str, font: str, size: float, box_w: float, box_h: float, multiline: bool
) -> tuple[str, ...] | None:
    ascent, descent = fonts.ascent_descent(font)
    if not multiline:
        if "\n" in text or "\r" in text:
            return None
        if fonts.width(text, font, size) > box_w - 2 * PAD_X or (ascent - descent) * size > box_h:
            return None
        return (text,)
    lines = _wrap(text, font, size, box_w - 2 * PAD_X)
    if lines is None:
        return None
    needed = (len(lines) - 1) * size * LINE_SPACING + (ascent - descent) * size
    return tuple(lines) if needed <= box_h - 2 * PAD_Y else None


def layout_text(text: str, box_w: float, box_h: float, style: TextStyle, multiline: bool) -> TextLayout:
    """Fit `text` into a box; raises TextOverflow instead of ever cutting anything off."""
    font, size = style.font, style.size
    while True:
        lines = _fits(text, font, size, box_w, box_h, multiline)
        if lines is not None:
            return TextLayout(font, size, lines, multiline)
        if not style.auto_fit or size - AUTO_FIT_STEP < style.min_size - 1e-9:
            raise TextOverflow()
        size = round(size - AUTO_FIT_STEP, 2)


@dataclass(frozen=True)
class TextItem:
    rect: VisibleRect
    layout: TextLayout
    style: TextStyle


@dataclass(frozen=True)
class MarkItem:
    rect: VisibleRect
    kind: str  # check | dot
    color: tuple[float, float, float] = (0.0, 0.0, 0.0)


@dataclass(frozen=True)
class ImageItem:
    rect: VisibleRect
    image: Image.Image  # RGBA, validated


@dataclass(frozen=True)
class Watermark:
    text: str = "PREVIEW"


def fit_image(rect: VisibleRect, width_px: int, height_px: int) -> VisibleRect:
    """Largest rectangle with the image's aspect ratio inside `rect`, centred (top-left system)."""
    scale = min(rect.width / width_px, rect.height / height_px)
    w, h = width_px * scale, height_px * scale
    return VisibleRect(rect.x + (rect.width - w) / 2, rect.y + (rect.height - h) / 2, w, h)


def _draw_text(c: canvas.Canvas, page_h: float, item: TextItem) -> None:
    r, lay, st = item.rect, item.layout, item.style
    ascent, descent = fonts.ascent_descent(lay.font)
    c.setFont(lay.font, lay.size)
    c.setFillColorRGB(*st.color)
    top = page_h - r.y  # display coordinates
    bottom = top - r.height
    if lay.multiline:
        baseline = top - PAD_Y - ascent * lay.size
    else:
        baseline = bottom + (r.height - (ascent - descent) * lay.size) / 2 - descent * lay.size
    for line in lay.lines:
        if st.align == "center":
            c.drawCentredString(r.x + r.width / 2, baseline, line)
        elif st.align == "right":
            c.drawRightString(r.x + r.width - PAD_X, baseline, line)
        else:
            c.drawString(r.x + PAD_X, baseline, line)
        baseline -= lay.size * LINE_SPACING


def _draw_mark(c: canvas.Canvas, page_h: float, item: MarkItem) -> None:
    r = item.rect
    side = min(r.width, r.height)
    x0 = r.x + (r.width - side) / 2
    y0 = page_h - r.y - r.height + (r.height - side) / 2
    if item.kind == "dot":
        c.setFillColorRGB(*item.color)
        c.circle(x0 + side / 2, y0 + side / 2, side * 0.25, stroke=0, fill=1)
        return
    c.setStrokeColorRGB(*item.color)
    c.setLineWidth(max(side * 0.12, 0.5))
    c.setLineCap(1)
    c.setLineJoin(1)
    path = c.beginPath()
    path.moveTo(x0 + side * 0.2, y0 + side * 0.52)
    path.lineTo(x0 + side * 0.42, y0 + side * 0.28)
    path.lineTo(x0 + side * 0.8, y0 + side * 0.76)
    c.drawPath(path, stroke=1, fill=0)


def _draw_image(c: canvas.Canvas, page_h: float, item: ImageItem) -> None:
    placed = fit_image(item.rect, *item.image.size)
    c.drawImage(
        ImageReader(item.image),
        placed.x,
        page_h - placed.y - placed.height,
        placed.width,
        placed.height,
        mask="auto",
    )


def _draw_watermark(c: canvas.Canvas, page_w: float, page_h: float, item: Watermark) -> None:
    c.saveState()
    c.setFont(fonts.font_name("sans", "bold"), min(page_w, page_h) / 6)
    c.setFillColorRGB(0.85, 0.1, 0.1, alpha=0.18)
    c.translate(page_w / 2, page_h / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, item.text)
    c.restoreState()


def render_overlay(pages: list[tuple[float, float, list]]) -> bytes:
    """One overlay page per entry (display width, height, items). Deterministic output."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, invariant=1, pageCompression=1)
    for page_w, page_h, items in pages:
        c.setPageSize((page_w, page_h))
        for item in items:
            if isinstance(item, ImageItem):
                _draw_image(c, page_h, item)
        for item in items:
            if isinstance(item, TextItem):
                _draw_text(c, page_h, item)
            elif isinstance(item, MarkItem):
                _draw_mark(c, page_h, item)
        for item in items:
            if isinstance(item, Watermark):
                _draw_watermark(c, page_w, page_h, item)
        c.showPage()
    c.save()
    return buf.getvalue()
