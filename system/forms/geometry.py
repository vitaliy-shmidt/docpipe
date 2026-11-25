"""Visible-page coordinate system of the PDF4Fill contract.

Every position DocPipe returns for forms is expressed on the page as a viewer
shows it:

- unit: PDF points;
- area: the visible page = CropBox intersected with MediaBox (inherited values
  resolved through the page tree);
- origin: top-left of the visible page, y grows downwards;
- page rotation (/Rotate 0/90/180/270, clockwise) already applied, so
  `width`/`height` of a page are the displayed dimensions.

PDF user space (where widget /Rect and text coordinates live) is unrotated
with a bottom-left origin. `PageBox.to_visible()` performs the full mapping.
"""

from __future__ import annotations

from dataclasses import dataclass

import pikepdf

_INHERITABLE_MAX_DEPTH = 64


def _inherited(page_obj: pikepdf.Dictionary, key: str):
    node = page_obj
    for _ in range(_INHERITABLE_MAX_DEPTH):
        if node is None:
            return None
        if key in node:
            return node[key]
        node = node.get("/Parent")
    return None


def _rect(value) -> tuple[float, float, float, float] | None:
    try:
        x0, y0, x1, y1 = (float(v) for v in value)
    except (TypeError, ValueError):
        return None
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


@dataclass(frozen=True)
class VisibleRect:
    x: float
    y: float
    width: float
    height: float

    def as_dict(self) -> dict:
        return {"x": round(self.x, 2), "y": round(self.y, 2), "width": round(self.width, 2),
                "height": round(self.height, 2)}


@dataclass(frozen=True)
class PageBox:
    """Effective visible box and rotation of one page (user space)."""

    left: float
    bottom: float
    right: float
    top: float
    rotation: int  # normalized to 0/90/180/270
    rotation_valid: bool  # False if /Rotate was not a multiple of 90 (treated as 0)
    mediabox: tuple[float, float, float, float]

    @classmethod
    def from_page(cls, page_obj: pikepdf.Dictionary) -> PageBox:
        media = _rect(_inherited(page_obj, "/MediaBox")) or (0.0, 0.0, 612.0, 792.0)
        crop = _rect(_inherited(page_obj, "/CropBox")) or media
        # The visible area is the CropBox clipped to the MediaBox (ISO 32000-1, 14.11.2).
        left, bottom = max(crop[0], media[0]), max(crop[1], media[1])
        right, top = min(crop[2], media[2]), min(crop[3], media[3])
        if right <= left or top <= bottom:
            left, bottom, right, top = media
        raw_rotation = _inherited(page_obj, "/Rotate")
        try:
            rotation = int(raw_rotation) if raw_rotation is not None else 0
        except (TypeError, ValueError):
            rotation = 1  # forces the invalid branch below
        valid = rotation % 90 == 0
        return cls(left, bottom, right, top, rotation % 360 if valid else 0, valid, media)

    @property
    def unrotated_width(self) -> float:
        return self.right - self.left

    @property
    def unrotated_height(self) -> float:
        return self.top - self.bottom

    @property
    def width(self) -> float:
        return self.unrotated_height if self.rotation in (90, 270) else self.unrotated_width

    @property
    def height(self) -> float:
        return self.unrotated_width if self.rotation in (90, 270) else self.unrotated_height

    def to_visible(self, llx: float, lly: float, urx: float, ury: float) -> VisibleRect:
        """Map a user-space rectangle to the visible, rotated, top-left page system."""
        x0, x1 = sorted((llx - self.left, urx - self.left))
        y0, y1 = sorted((lly - self.bottom, ury - self.bottom))
        w, h = self.unrotated_width, self.unrotated_height
        if self.rotation == 0:
            return VisibleRect(x0, h - y1, x1 - x0, y1 - y0)
        if self.rotation == 90:
            return VisibleRect(y0, x0, y1 - y0, x1 - x0)
        if self.rotation == 180:
            return VisibleRect(w - x1, y0, x1 - x0, y1 - y0)
        return VisibleRect(h - y1, w - x1, y1 - y0, x1 - x0)  # 270

    def clip(self, rect: VisibleRect, keep_lines: bool = False) -> tuple[VisibleRect | None, bool]:
        """Clip to the visible page. Returns (rect or None if fully outside, was_clipped).

        A rectangle without area counts as outside - except, with `keep_lines`, a
        line of zero thickness (stroked rules in the page layout) that lies on the page.
        """
        tolerance = 0.5
        x0, y0 = max(rect.x, 0.0), max(rect.y, 0.0)
        x1, y1 = min(rect.x + rect.width, self.width), min(rect.y + rect.height, self.height)
        w, h = x1 - x0, y1 - y0
        if w < 0 or h < 0 or (w == 0 and h == 0) or (not keep_lines and (w == 0 or h == 0)):
            return None, True
        clipped = (
            rect.x < -tolerance
            or rect.y < -tolerance
            or rect.x + rect.width > self.width + tolerance
            or rect.y + rect.height > self.height + tolerance
        )
        if not clipped:
            return rect, False
        return VisibleRect(x0, y0, x1 - x0, y1 - y0), True
