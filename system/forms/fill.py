"""Filling a PDF form template (Phase 2.2, PDF4Fill contract schema_version "1").

Pure function: template bytes + manifest + PNG images (+ password) in, a new,
flattened, print-ready PDF out. The template bytes are never modified; nothing
is persisted or logged here. Error `details` carry field ids and reasons only,
never submitted values.

Order of work:
1. manifest (schema) and SHA-256 of the template;
2. protection: open password, permissions (no bypass), signatures/DocMDP,
   dynamic XFA;
3. every manifest entry against the document: native entries by `native_ref`
   (the inspect identity), overlay entries by page + position; types, options,
   required values, lengths, geometry, conflicts;
4. layout of every text value with the bundled fonts (system/forms/render.py)
   - overflow and unprintable characters are errors, nothing is cut off;
5. native checkboxes/radios via their own appearance states, native text via
   the shared renderer on top of the widget's border/background (its old
   variable text is removed), then all widget appearances are flattened and
   the form is removed;
6. one reportlab overlay page per template page, placed with the matrix of
   the page's CropBox and /Rotate (display coordinates -> user space);
7. saved with the template's encryption and permissions preserved.

Preview (`output.preview`) runs exactly the same steps and only adds a
watermark item to every overlay page.
"""

from __future__ import annotations

import hashlib
import io
from collections import defaultdict
from dataclasses import dataclass

import pikepdf
from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from system.errors import DocPipeError
from system.forms import fonts
from system.forms import render as rnd
from system.forms.geometry import PageBox, VisibleRect
from system.forms.inspect import NativeWidget, _open, native_widgets
from system.forms.schemas import FillField, FillManifest, FillStyle

MAX_MANIFEST_BYTES = 1024 * 1024
MAX_IMAGES = 10
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_IMAGE_PIXELS = 25_000_000
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_POSITION_TOLERANCE = 0.5
_TEXT_TYPES = {"text", "number", "date"}
_IMAGE_TYPES = {"stamp_image", "signature_image"}
_NATIVE_TYPES = {
    "text": "text",
    "number": "text",
    "date": "text",
    "choice": "choice",
    "checkbox": "checkbox",
    "radio": "radio",
}
_F_HIDDEN, _F_NOVIEW = 1 << 1, 1 << 5
_OVERLAY_NAME = "/DocPipeFill"


@dataclass(frozen=True)
class FillResult:
    pdf: bytes
    native_fields: int
    overlay_fields: int
    images: int
    pages: int
    encrypted: bool


def _err(code: str, message: str, **details) -> DocPipeError:
    return DocPipeError(code, message, details={k: v for k, v in details.items() if v is not None} or None)


# --- input --------------------------------------------------------------------------------------


def parse_manifest(raw: str | bytes) -> FillManifest:
    data = raw.encode("utf-8") if isinstance(raw, str) else raw
    if len(data) > MAX_MANIFEST_BYTES:
        raise _err("limit_exceeded", "The manifest is too large.")
    try:
        return FillManifest.model_validate_json(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        path = ".".join(str(p) for p in first.get("loc", ())) or None
        raise _err("invalid_manifest", "The manifest is invalid.", path=path) from None


def load_images(parts: dict[str, bytes]) -> dict[str, Image.Image]:
    """Validate PNG uploads (signature, decodable, size) and convert them to RGBA."""
    if len(parts) > MAX_IMAGES:
        raise _err("limit_exceeded", "Too many images.")
    images: dict[str, Image.Image] = {}
    for ref, data in parts.items():
        if len(data) > MAX_IMAGE_BYTES:
            raise _err("limit_exceeded", "An image is too large.", image_ref=ref)
        if not data.startswith(_PNG_MAGIC):
            raise _err("invalid_image", "Images must be PNG files.", image_ref=ref)
        try:
            with Image.open(io.BytesIO(data)) as probe:
                if probe.format != "PNG":
                    raise _err("invalid_image", "Images must be PNG files.", image_ref=ref)
                if probe.width * probe.height > MAX_IMAGE_PIXELS or probe.width < 1 or probe.height < 1:
                    raise _err("invalid_image", "The image has an unsupported size.", image_ref=ref)
                probe.verify()
            with Image.open(io.BytesIO(data)) as image:
                images[ref] = image.convert("RGBA")
        except DocPipeError:
            raise
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
            raise _err("invalid_image", "The image could not be read.", image_ref=ref) from None
    return images


# --- helpers ------------------------------------------------------------------------------------


def _color(hex_color: str) -> tuple[float, float, float]:
    return tuple(int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))  # type: ignore[return-value]


def _text_style(style: FillStyle) -> rnd.TextStyle:
    if style.min_font_size > style.font_size:
        raise ValueError("min_font_size")
    return rnd.TextStyle(
        family=style.font_family,
        size=style.font_size,
        color=_color(style.color),
        weight=style.font_weight,
        style=style.font_style,
        align=style.align,
        auto_fit=style.auto_fit,
        min_size=style.min_font_size,
    )


def _rect(position) -> VisibleRect:
    return VisibleRect(position.x, position.y, position.width, position.height)


def _overlap_share(a: VisibleRect, b: VisibleRect) -> float:
    w = min(a.x + a.width, b.x + b.width) - max(a.x, b.x)
    h = min(a.y + a.height, b.y + b.height) - max(a.y, b.y)
    if w <= 0 or h <= 0:
        return 0.0
    return (w * h) / min(a.width * a.height, b.width * b.height)


def _is_empty(value) -> bool:
    return value is None or value is False or value == "" or value == []


def _hidden(widget: pikepdf.Dictionary) -> bool:
    return bool(int(widget.get("/F", 0)) & (_F_HIDDEN | _F_NOVIEW))


def _states(widget: pikepdf.Dictionary) -> list[str]:
    ap = widget.get("/AP")
    normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
    if not isinstance(normal, pikepdf.Dictionary):
        return []
    return [str(k)[1:] for k in normal.keys() if str(k) != "/Off"]


def _strip_variable_text(widget: pikepdf.Dictionary) -> None:
    """Remove the `/Tx BMC ... EMC` variable-text section from the normal appearance (keeps borders)."""
    ap = widget.get("/AP")
    normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
    streams = (
        [normal]
        if isinstance(normal, pikepdf.Stream)
        else (
            [v for v in normal.values() if isinstance(v, pikepdf.Stream)]
            if isinstance(normal, pikepdf.Dictionary)
            else []
        )
    )
    for stream in streams:
        try:
            instructions = pikepdf.parse_content_stream(stream)
        except pikepdf.PdfError:
            continue
        kept, depth = [], 0
        for instruction in instructions:
            op = str(instruction.operator)
            if depth:
                if op in ("BMC", "BDC"):
                    depth += 1
                elif op == "EMC":
                    depth -= 1
                continue
            if op == "BMC" and instruction.operands and str(instruction.operands[0]) == "/Tx":
                depth = 1
                continue
            kept.append(instruction)
        stream.write(pikepdf.unparse_content_stream(kept))


def _display_matrix(box: PageBox) -> str:
    """Display coordinates (visible page, bottom-left origin) -> user space, as a `cm` operand."""
    left, bottom, right, top = box.left, box.bottom, box.right, box.top
    return {
        0: f"1 0 0 1 {left} {bottom}",
        90: f"0 1 -1 0 {right} {bottom}",
        180: f"-1 0 0 -1 {right} {top}",
        270: f"0 -1 1 0 {left} {top}",
    }[box.rotation]


# --- validation and planning -----------------------------------------------------------------------


@dataclass
class _Plan:
    items: dict[int, list]  # 0-based page -> render items
    native_count: int = 0
    overlay_count: int = 0
    image_count: int = 0


def _layout(entry: FillField, text: str, rect: VisibleRect, multiline: bool) -> rnd.TextItem:
    try:
        style = _text_style(entry.style)
    except ValueError:
        raise _err(
            "invalid_manifest", "min_font_size must not exceed font_size.", field_id=entry.id
        ) from None
    if fonts.unsupported_characters(text, style.font):
        raise _err(
            "unsupported_characters",
            "The value contains characters the selected font cannot print.",
            field_id=entry.id,
            count=len(fonts.unsupported_characters(text, style.font)),
        )
    try:
        layout = rnd.layout_text(text, rect.width, rect.height, style, multiline)
    except rnd.TextOverflow:
        raise _err("text_overflow", "The value does not fit into the field.", field_id=entry.id) from None
    return rnd.TextItem(rect, layout, style)


def _check_value(entry: FillField, kind: str, item: dict | None) -> None:
    value = entry.value
    required = entry.required or bool(item and item.get("required"))
    if required and _is_empty(value):
        raise _err(
            "invalid_value", "A required field has no value.", field_id=entry.id, reason="missing_value"
        )
    if kind in _TEXT_TYPES:
        if value is not None and not isinstance(value, str):
            raise _err("invalid_value", "The value must be a string.", field_id=entry.id, reason="type")
        max_length = item.get("max_length") if item else None
        if max_length is not None and value and len(value) > max_length:
            raise _err(
                "invalid_value",
                "The value is longer than the field allows.",
                field_id=entry.id,
                reason="too_long",
            )
    elif kind == "choice":
        options = [o["value"] for o in (item or {}).get("options", [])]
        values = value if isinstance(value, list) else ([] if value in (None, "") else [value])
        if isinstance(value, bool) or not all(isinstance(v, str) for v in values):
            raise _err("invalid_value", "The value must be a string.", field_id=entry.id, reason="type")
        if isinstance(value, list) and not (item or {}).get("multi_select"):
            raise _err("invalid_value", "The field allows one value only.", field_id=entry.id, reason="type")
        if not (item or {}).get("editable") and any(v not in options for v in values):
            raise _err(
                "invalid_value",
                "The value is not one of the field's options.",
                field_id=entry.id,
                reason="option",
            )
    elif kind == "checkbox":
        if isinstance(value, list):
            raise _err(
                "invalid_value",
                "A checkbox takes true/false or its export value.",
                field_id=entry.id,
                reason="type",
            )
    elif kind == "radio":
        if value is not None and not isinstance(value, str):
            raise _err(
                "invalid_value", "A radio group takes one export value.", field_id=entry.id, reason="type"
            )


def _plan(
    manifest: FillManifest,
    natives: list[NativeWidget],
    boxes: list[PageBox],
    images: dict[str, Image.Image],
    preview: bool,
) -> _Plan:
    plan = _Plan(items=defaultdict(list))
    by_ref = {n.item["native_ref"]: n for n in natives}
    by_widget = {n.widget.objgen: n for n in natives}
    ids: set[str] = set()
    used_refs: set[str] = set()
    field_values: dict[tuple, tuple[FillField, object]] = {}  # field objgen -> (first entry, value)
    native_rects: dict[int, list[tuple[VisibleRect, str]]] = defaultdict(list)
    overlays: list[tuple[FillField, int, VisibleRect]] = []

    for entry in manifest.fields:
        if entry.id in ids:
            raise _err("invalid_manifest", "Field ids must be unique.", field_id=entry.id)
        ids.add(entry.id)
        if entry.mode == "native":
            if entry.type in _IMAGE_TYPES:
                raise _err(
                    "invalid_manifest", "Images can only be placed with mode=overlay.", field_id=entry.id
                )
            native = by_ref.get(entry.native_ref or "")
            if native is None:
                raise _err(
                    "unknown_field",
                    "native_ref does not identify a field of this template.",
                    field_id=entry.id,
                )
            item = native.item
            if entry.native_ref in used_refs:
                raise _err(
                    "field_conflict",
                    "The same widget is listed twice.",
                    field_id=entry.id,
                    reason="duplicate_native_ref",
                )
            used_refs.add(entry.native_ref)
            if item["type"] == "signature" or _NATIVE_TYPES[entry.type] != item["type"]:
                raise _err(
                    "field_conflict",
                    "The type does not match the native field.",
                    field_id=entry.id,
                    reason="type_mismatch",
                )
            if entry.input_name is not None and entry.input_name != item["input_name"]:
                raise _err(
                    "field_conflict",
                    "input_name does not match the native field.",
                    field_id=entry.id,
                    reason="input_name_mismatch",
                )
            if entry.page is not None and entry.page != item["page"]:
                raise _err(
                    "field_conflict",
                    "The page does not match the native field.",
                    field_id=entry.id,
                    reason="geometry_mismatch",
                )
            if entry.position is not None:
                p = item["position"]
                given = entry.position
                if any(
                    abs(a - b) > _POSITION_TOLERANCE
                    for a, b in (
                        (given.x, p["x"]),
                        (given.y, p["y"]),
                        (given.width, p["width"]),
                        (given.height, p["height"]),
                    )
                ):
                    raise _err(
                        "field_conflict",
                        "A changed position of a native field needs mode=overlay.",
                        field_id=entry.id,
                        reason="geometry_mismatch",
                    )
            _check_value(entry, entry.type if entry.type != "choice" else "choice", item)
            key = native.field.objgen
            normalized = entry.value
            if entry.type == "checkbox" and isinstance(normalized, str) and normalized == "":
                normalized = False
            if key in field_values:
                first, value = field_values[key]
                if value != normalized:
                    raise _err(
                        "field_conflict",
                        "Widgets of one field have different values.",
                        field_id=entry.id,
                        reason="inconsistent_value",
                    )
                continue
            field_values[key] = (entry, normalized)
            _plan_native(plan, entry, native, normalized, by_widget, boxes, native_rects)
        else:
            if entry.page is None or entry.position is None:
                raise _err("invalid_manifest", "mode=overlay needs page and position.", field_id=entry.id)
            if entry.type in ("radio", "choice"):
                raise _err("invalid_manifest", "radio and choice need mode=native.", field_id=entry.id)
            if entry.page > len(boxes):
                raise _err("invalid_geometry", "The page does not exist.", field_id=entry.id, reason="page")
            box, rect = boxes[entry.page - 1], _rect(entry.position)
            if (
                rect.x + rect.width > box.width + _POSITION_TOLERANCE
                or rect.y + rect.height > box.height + _POSITION_TOLERANCE
            ):
                raise _err(
                    "invalid_geometry",
                    "The position lies outside the visible page.",
                    field_id=entry.id,
                    reason="outside_page",
                )
            overlays.append((entry, entry.page - 1, rect))

    for entry, index, rect in overlays:
        for native_rect, _native_id in native_rects.get(index, []):
            if _overlap_share(rect, native_rect) > 0.5:
                raise _err(
                    "field_conflict",
                    "An overlay covers a native field that is filled as well.",
                    field_id=entry.id,
                    reason="overlaps_native",
                )
        _plan_overlay(plan, entry, index, rect, images)

    if preview:
        for index in range(len(boxes)):
            plan.items[index].append(rnd.Watermark())
    return plan


def _plan_native(
    plan: _Plan, entry: FillField, native: NativeWidget, value, by_widget, boxes, native_rects
) -> None:
    item = native.item
    siblings = [by_widget[w.objgen] for w in native.siblings if w.objgen in by_widget]
    plan.native_count += 1
    if item["type"] in ("text", "choice"):
        if isinstance(value, list):
            text, multiline = "\n".join(_label(item, v) for v in value), True
        else:
            text = "" if value is None else (_label(item, value) if item["type"] == "choice" else value)
            multiline = (
                bool(item.get("multiline")) if entry.style.multiline is None else entry.style.multiline
            )
        for sibling in siblings:
            _strip_variable_text(sibling.widget)
            if _hidden(sibling.widget) or text == "":
                continue
            rect = _rect_of(sibling)
            plan.items[sibling.item["page"] - 1].append(_layout(entry, text, rect, multiline))
            native_rects[sibling.item["page"] - 1].append((rect, entry.id))
        return

    # checkbox / radio: the template's own appearance states
    if item["type"] == "checkbox":
        if value is True:
            target = item.get("export_value") or next((o["value"] for o in item.get("options", [])), "Yes")
        elif isinstance(value, str) and value:
            if value not in [o["value"] for o in item.get("options", [])]:
                raise _err(
                    "invalid_value",
                    "The value is not the checkbox's export value.",
                    field_id=entry.id,
                    reason="option",
                )
            target = value
        else:
            target = None
    else:
        if value is not None and value not in [o["value"] for o in item.get("options", [])]:
            raise _err(
                "invalid_value",
                "The value is not one of the radio group's options.",
                field_id=entry.id,
                reason="option",
            )
        target = value
    color = _color(entry.style.color)
    for sibling in siblings:
        widget = sibling.widget
        states = _states(widget)
        # A radio button is on only if it owns the chosen state; a checkbox widget is on for any target.
        on = target is not None and (target in states if item["type"] == "radio" else True)
        widget.AS = pikepdf.Name("/" + target) if on and target in states else pikepdf.Name.Off
        rect = _rect_of(sibling)
        native_rects[sibling.item["page"] - 1].append((rect, entry.id))
        if on and target not in states and not _hidden(widget):  # no appearance for the state: vector mark
            kind = "dot" if item["type"] == "radio" else "check"
            plan.items[sibling.item["page"] - 1].append(rnd.MarkItem(rect, kind, color))


def _label(item: dict, value: str) -> str:
    for option in item.get("options", []):
        if option["value"] == value:
            return option["label"]
    return value


def _rect_of(native: NativeWidget) -> VisibleRect:
    p = native.item["position"]
    return VisibleRect(p["x"], p["y"], p["width"], p["height"])


def _plan_overlay(plan: _Plan, entry: FillField, index: int, rect: VisibleRect, images) -> None:
    plan.overlay_count += 1
    if entry.type in _IMAGE_TYPES:
        if entry.image_ref is None or entry.image_ref not in images:
            raise _err("invalid_manifest", "image_ref does not name an uploaded image.", field_id=entry.id)
        plan.items[index].append(rnd.ImageItem(rect, images[entry.image_ref]))
        plan.image_count += 1
        return
    _check_value(entry, entry.type, None)
    if entry.type == "checkbox":
        if entry.value is True or (isinstance(entry.value, str) and entry.value):
            plan.items[index].append(rnd.MarkItem(rect, "check", _color(entry.style.color)))
        return
    text = entry.value or ""
    if text:
        multiline = bool(entry.style.multiline)
        plan.items[index].append(_layout(entry, text, rect, multiline))


# --- document ------------------------------------------------------------------------------------


def _check_protection(pdf: pikepdf.Pdf, natives: list[NativeWidget], has_native_entries: bool) -> None:
    if pdf.Root.get("/NeedsRendering", False):
        raise _err("xfa_unsupported", "Dynamic XFA forms are not supported.")
    acroform = pdf.Root.get("/AcroForm")
    if isinstance(acroform, pikepdf.Dictionary) and "/XFA" in acroform and not natives:
        raise _err("xfa_unsupported", "Dynamic XFA forms are not supported.")
    signed = any(n.item["type"] == "signature" and n.item.get("signed") for n in natives)
    perms = pdf.Root.get("/Perms")
    if signed or (isinstance(perms, pikepdf.Dictionary) and "/DocMDP" in perms):
        raise _err(
            "signature_conflict",
            "Signed or certified documents are not changed.",
            reason="certified" if not signed else "signed",
        )
    if pdf.is_encrypted and not getattr(pdf, "owner_password_matched", False):
        allow = pdf.allow
        if has_native_entries and not allow.modify_form:
            raise _err(
                "permission_denied",
                "The PDF's permissions do not allow filling in form fields.",
                permission="fill_forms",
            )
        if not allow.modify_other:
            raise _err(
                "permission_denied",
                "The PDF's permissions do not allow changing the document "
                "(required for the flattened output).",
                permission="modify",
            )


def _flatten(pdf: pikepdf.Pdf) -> None:
    acroform = pdf.Root.get("/AcroForm")
    if isinstance(acroform, pikepdf.Dictionary):
        acroform.NeedAppearances = False  # never let qpdf regenerate appearances (Helvetica only)
    pdf.flatten_annotations("all")
    for page in pdf.pages:
        annots = page.obj.get("/Annots")
        if annots is None:
            continue
        kept = [
            a
            for a in annots
            if not (isinstance(a, pikepdf.Dictionary) and a.get("/Subtype") == pikepdf.Name.Widget)
        ]
        if kept:
            page.obj.Annots = pikepdf.Array(kept)
        else:
            del page.obj["/Annots"]
    if "/AcroForm" in pdf.Root:
        del pdf.Root["/AcroForm"]
    if "/NeedsRendering" in pdf.Root:
        del pdf.Root["/NeedsRendering"]


def _place_overlays(pdf: pikepdf.Pdf, boxes: list[PageBox], items: dict[int, list]) -> None:
    indexes = sorted(i for i, page_items in items.items() if page_items)
    if not indexes:
        return
    overlay_bytes = rnd.render_overlay([(boxes[i].width, boxes[i].height, items[i]) for i in indexes])
    overlay = pikepdf.open(io.BytesIO(overlay_bytes))
    try:
        for position, index in enumerate(indexes):
            page = pdf.pages[index]
            xobject = pdf.copy_foreign(overlay.pages[position].as_form_xobject())
            # deterministic resource name (add_resource's prefix mode appends a random suffix)
            existing = set(page.resources.get("/XObject", {}).keys())
            name = next(
                f"{_OVERLAY_NAME}{n or ''}"
                for n in range(0, 1000)
                if f"{_OVERLAY_NAME}{n or ''}" not in existing
            )
            name = page.add_resource(
                xobject, pikepdf.Name.XObject, pikepdf.Name(name), replace_existing=False
            )
            page.contents_add(pdf.make_stream(b"q\n"), prepend=True)
            page.contents_add(
                pdf.make_stream(
                    b"\nQ\nq "
                    + _display_matrix(boxes[index]).encode("ascii")
                    + b" cm "
                    + str(name).encode("ascii")
                    + b" Do Q\n"
                )
            )
    finally:
        overlay.close()


def fill_pdf(
    data: bytes,
    manifest: FillManifest,
    images: dict[str, Image.Image],
    password: str | None = None,
    *,
    max_pages: int,
    max_fields: int,
) -> FillResult:
    """Fill and flatten one template. Raises DocPipeError for anything that cannot be filled as asked."""
    if hashlib.sha256(data).hexdigest() != manifest.sha256:
        raise _err("hash_mismatch", "The template does not match the manifest's sha256.")
    if len(manifest.fields) > max_fields:
        raise _err("limit_exceeded", "The manifest has more fields than allowed.")
    pdf = _open(data, password)
    try:
        page_count = len(pdf.pages)
        if page_count == 0:
            raise DocPipeError("invalid_file", "The PDF has no pages.")
        if page_count > max_pages:
            raise DocPipeError("limit_exceeded", "The PDF has more pages than allowed.")
        boxes = [PageBox.from_page(page.obj) for page in pdf.pages]
        natives = native_widgets(pdf, boxes, [], max_fields)
        _check_protection(pdf, natives, any(f.mode == "native" for f in manifest.fields))
        plan = _plan(manifest, natives, boxes, images, manifest.output.preview)
        encrypted = bool(pdf.is_encrypted)

        _flatten(pdf)
        _place_overlays(pdf, boxes, plan.items)

        out = io.BytesIO()
        if encrypted:
            pdf.save(out, encryption=True)  # same passwords, method and permissions as the template
        else:
            pdf.save(out, encryption=False, deterministic_id=True)
        return FillResult(
            out.getvalue(), plan.native_count, plan.overlay_count, plan.image_count, page_count, encrypted
        )
    finally:
        pdf.close()
