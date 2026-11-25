"""OCR for pages without a text layer (Phase 2.1b), via the existing Stirling client.

Only the pages that need it are sent: a decrypted, in-memory subset PDF whose
pages carry explicit MediaBox/CropBox/Rotate (inherited values resolved). The
password never leaves DocPipe. Stirling's answer is untrusted input and is
checked before anything is used.

Stirling 3.2.2 has two OCR paths (OCRController):
- OCRmyPDF: adds an invisible text layer to the ORIGINAL page - MediaBox,
  CropBox and /Rotate stay identical ("identical");
- Tesseract fallback: renders the visible page at 300 dpi and builds a NEW page
  of that size, unrotated, origin 0/0 ("rescaled": the new page IS the visible
  page, up to pixel rounding).

Any other output geometry is not mapped onto the template: the page gets its
OCR text but no positions, plus the warning `ocr_geometry_mismatch` - never a
guessed position.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from dataclasses import dataclass

import pikepdf

from system.errors import DocPipeError
from system.forms.geometry import PageBox, VisibleRect
from system.forms.layout import PostMap, identity

# bytes of the subset PDF -> bytes of the OCR'd PDF (raises DocPipeError on upstream failure)
OcrRunner = Callable[[bytes], bytes]

_BOX_TOLERANCE = 0.05  # points: "identical" boxes
_SIZE_TOLERANCE = 0.02  # 2 % of the visible size for the rendered (rescaled) page
_ASPECT_TOLERANCE = 0.01


@dataclass(frozen=True)
class OcrPage:
    page: int  # 1-based page of the template
    geometry: str  # identical | rescaled | mismatch
    post: PostMap  # visible rect on the OCR page -> visible rect on the template page


@dataclass(frozen=True)
class OcrResult:
    pdf: bytes  # Stirling's output, validated
    pages: dict[int, OcrPage]  # 0-based index in `pdf` -> page info


def build_subset(pdf: pikepdf.Pdf, boxes: list[PageBox], indexes: list[int]) -> bytes:
    """Unencrypted PDF of the given 0-based pages with resolved page boxes and rotation."""
    subset = pikepdf.new()
    for index in indexes:
        page, box = pdf.pages[index], boxes[index]
        page.obj.MediaBox = pikepdf.Array(list(box.mediabox))
        page.obj.CropBox = pikepdf.Array([box.left, box.bottom, box.right, box.top])
        page.obj.Rotate = box.rotation
        subset.pages.append(page)
    out = io.BytesIO()
    subset.save(out)
    return out.getvalue()


def _no_position(_rect: VisibleRect) -> None:
    return None


def _same(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance


def _rescale(template: PageBox, out: PageBox) -> PostMap:
    sx, sy = template.width / out.width, template.height / out.height

    def post(rect: VisibleRect) -> VisibleRect | None:
        scaled = VisibleRect(rect.x * sx, rect.y * sy, rect.width * sx, rect.height * sy)
        visible, _ = template.clip(scaled, keep_lines=True)
        return visible

    return post


def classify(template: PageBox, out: PageBox) -> tuple[str, PostMap]:
    """How the visible page of an OCR output page relates to the template's visible page."""
    template_visible = (template.left, template.bottom, template.right, template.top)
    out_visible = (out.left, out.bottom, out.right, out.top)
    if (
        all(_same(a, b, _BOX_TOLERANCE) for a, b in zip(template.mediabox, out.mediabox))
        and all(_same(a, b, _BOX_TOLERANCE) for a, b in zip(template_visible, out_visible))
        and template.rotation == out.rotation
    ):
        return "identical", identity
    if out.rotation == 0 and out.width > 0 and out.height > 0:
        aspect_t, aspect_o = template.width / template.height, out.width / out.height
        if (
            abs(aspect_t - aspect_o) <= _ASPECT_TOLERANCE * aspect_t
            and _same(out.width, template.width, _SIZE_TOLERANCE * template.width)
            and _same(out.height, template.height, _SIZE_TOLERANCE * template.height)
        ):
            return "rescaled", _rescale(template, out)
    return "mismatch", _no_position


def run(pdf: pikepdf.Pdf, boxes: list[PageBox], indexes: list[int], runner: OcrRunner) -> OcrResult:
    """OCR the given 0-based template pages.

    Raises DocPipeError: the runner's own upstream errors unchanged, and
    `ocr_invalid_output` when Stirling's answer is no usable PDF or has a
    different page count (pages could not be assigned reliably).
    """
    output = runner(build_subset(pdf, boxes, indexes))
    if not output.startswith(b"%PDF-"):
        raise DocPipeError("ocr_invalid_output", "OCR returned no PDF.")
    try:
        ocr_pdf = pikepdf.open(io.BytesIO(output))
    except (pikepdf.PdfError, ValueError, OSError):
        raise DocPipeError("ocr_invalid_output", "OCR returned an unreadable PDF.") from None
    try:
        if ocr_pdf.is_encrypted:
            raise DocPipeError("ocr_invalid_output", "OCR returned an encrypted PDF.")
        if len(ocr_pdf.pages) != len(indexes):
            raise DocPipeError("ocr_invalid_output", "OCR returned a different number of pages.")
        pages: dict[int, OcrPage] = {}
        for position, (template_index, page) in enumerate(zip(indexes, ocr_pdf.pages)):
            geometry, post = classify(boxes[template_index], PageBox.from_page(page.obj))
            pages[position] = OcrPage(template_index + 1, geometry, post)
    finally:
        ocr_pdf.close()
    return OcrResult(output, pages)
