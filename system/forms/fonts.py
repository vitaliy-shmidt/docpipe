"""Fonts for forms/fill (Phase 2.2).

Only the bundled Liberation fonts are used (system/forms/fonts/, SIL Open Font
License 1.1, see LICENSE there): metric-compatible with Arial/Helvetica, Times
and Courier, so text sized for those in an existing form layout keeps its
width. Coverage: Latin incl. Central/Eastern European, Greek, Cyrillic, common
symbols (no check-mark glyphs - checkboxes are drawn as vector marks).

Fonts are registered with reportlab lazily and embedded as subsets into the
output, so every character that is printed exists in the PDF itself.
"""

from __future__ import annotations

import threading
from functools import lru_cache
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).resolve().parent / "fonts"

FAMILIES = {"sans": "LiberationSans", "serif": "LiberationSerif", "mono": "LiberationMono"}
DISPLAY_NAMES = {"sans": "Liberation Sans", "serif": "Liberation Serif", "mono": "Liberation Mono"}
_FACES = {("normal", "normal"): "Regular", ("bold", "normal"): "Bold",
          ("normal", "italic"): "Italic", ("bold", "italic"): "BoldItalic"}

# Characters never checked against the font: layout controls handled by the renderer.
_CONTROL = {"\n", "\r", "\t"}

_lock = threading.Lock()


@lru_cache(maxsize=None)
def font_name(family: str, weight: str = "normal", style: str = "normal") -> str:
    """reportlab font name of a bundled face (registered on first use)."""
    face = _FACES[(weight, style)]
    name = f"DocPipe-{FAMILIES[family]}-{face}"
    with _lock:
        if name not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont(name, str(FONT_DIR / f"{FAMILIES[family]}-{face}.ttf")))
    return name


def _face(name: str):
    return pdfmetrics.getFont(name).face


def unsupported_characters(text: str, name: str) -> list[str]:
    """Distinct characters of `text` the font cannot print (in order of appearance)."""
    cmap = _face(name).charToGlyph
    missing: list[str] = []
    for char in text:
        if char in _CONTROL or char == " ":
            continue
        if ord(char) not in cmap and char not in missing:
            missing.append(char)
    return missing


def width(text: str, name: str, size: float) -> float:
    return pdfmetrics.stringWidth(text, name, size)


def ascent_descent(name: str) -> tuple[float, float]:
    """Ascent and (negative) descent per 1 pt of font size."""
    face = _face(name)
    return face.ascent / 1000.0, face.descent / 1000.0
