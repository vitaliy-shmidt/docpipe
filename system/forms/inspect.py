"""Structural inspection of a PDF form template (PDF4Fill contract, schema_version "1").

Pure function: bytes (+ optional password) in, JSON-ready dict out. Nothing is
persisted, nothing is logged here - the route logs metadata only. Error
messages never contain names, values, text or the password.

What is returned (see README "POST /api/v1/documents/forms/inspect"):
- sha256 of the uploaded bytes (the template identity PDF4Fill stores);
- per page: visible size, rotation, text, text blocks (one per text line) with
  positions, and the text source (`embedded` or `none` - OCR is not part of
  this phase);
- one entry per native widget (a field with two widgets yields two entries
  sharing `field_ref`/`input_name`);
- security facts (encryption, permissions, signatures, XFA) and warnings.

Static/scanned PDFs are not analysed for empty areas in this phase: they
simply return an empty field list.
"""

from __future__ import annotations

import hashlib
import io
import logging
from dataclasses import dataclass, field

import pikepdf

from system.errors import DocPipeError
from system.forms import detect as detect_mod
from system.forms import layout as layout_mod
from system.forms import names as names_mod
from system.forms import ocr as ocr_mod
from system.forms.geometry import PageBox, VisibleRect

SCHEMA_VERSION = "1"

# pdfminer can be chatty about malformed content; nothing it logs is useful
# to DocPipe's operators and it must never end up next to document content.
logging.getLogger("pdfminer").setLevel(logging.ERROR)

# Field flags (ISO 32000-1, tables 221, 226, 228, 230). Bit n => 1 << (n - 1).
_FF_READ_ONLY = 1 << 0
_FF_REQUIRED = 1 << 1
_FF_TX_MULTILINE = 1 << 12
_FF_TX_PASSWORD = 1 << 13
_FF_BTN_RADIO = 1 << 15
_FF_BTN_PUSHBUTTON = 1 << 16
_FF_CH_COMBO = 1 << 17
_FF_CH_EDIT = 1 << 18
_FF_CH_MULTISELECT = 1 << 21
# Annotation flags (table 165)
_F_HIDDEN = 1 << 1
_F_NOVIEW = 1 << 5

_MAX_TREE_DEPTH = 32


@dataclass
class _Widget:
    obj: pikepdf.Dictionary
    page: int
    rect: VisibleRect


@dataclass
class _Field:
    obj: pikepdf.Dictionary
    full_name: str
    field_type: str | None
    flags: int
    widgets: list[pikepdf.Dictionary] = field(default_factory=list)


def _warning(code: str, message: str, **extra) -> dict:
    return {"code": code, "message": message, **extra}


def _ref(obj) -> str | None:
    try:
        objnum, gen = obj.objgen
    except (AttributeError, ValueError):
        return None
    return f"{objnum}.{gen}" if objnum else None


def _text(value) -> str | None:
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001 - undecodable string object
        return None


def _name(value) -> str:
    """JSON-safe, reversible text of a PDF name (bytes that are not UTF-8 become #XX; see forms/names.py)."""
    return names_mod.name_text(value)


def _open(data: bytes, password: str | None) -> pikepdf.Pdf:
    try:
        return pikepdf.open(io.BytesIO(data), password=password or "")
    except pikepdf.PasswordError:
        if password:
            raise DocPipeError("invalid_password", "The supplied password does not open this PDF.") from None
        raise DocPipeError("password_required", "This PDF requires a password to open.") from None
    except (pikepdf.PdfError, ValueError, OSError):
        raise DocPipeError("invalid_file", "Uploaded file is not a readable PDF.") from None


def _needs_open_password(data: bytes) -> bool:
    try:
        pikepdf.open(io.BytesIO(data)).close()
    except pikepdf.PasswordError:
        return True
    except (pikepdf.PdfError, ValueError, OSError):
        return False
    return False


def _security(pdf: pikepdf.Pdf, open_password_required: bool) -> dict:
    encrypted = bool(pdf.is_encrypted)
    result: dict = {"encrypted": encrypted, "open_password_required": encrypted and open_password_required,
                    "encryption_method": None, "permissions": None}
    if encrypted:
        info = pdf.encryption
        method = _name(getattr(info.stream_method, "name", info.stream_method)).lower()
        result["encryption_method"] = method
        allow = pdf.allow
        # The owner password grants every permission (ISO 32000-1, 7.6.3.2);
        # qpdf reports the /P restrictions regardless of which password matched.
        owner = bool(getattr(pdf, "owner_password_matched", False))
        result["permissions"] = {
            "extract_text": owner or bool(allow.extract),
            "fill_forms": owner or bool(allow.modify_form),
            "modify_annotations": owner or bool(allow.modify_annotation),
            "modify": owner or bool(allow.modify_other),
            "assemble": owner or bool(allow.modify_assembly),
            "print": owner or bool(allow.print_lowres or allow.print_highres),
        }
    return result


def _collect_fields(acroform, warnings: list[dict]) -> list[_Field]:
    """Walk the AcroForm field tree into terminal fields with their widgets."""
    fields: list[_Field] = []
    seen: set[tuple[int, int]] = set()
    cycle_reported = False

    def walk(node, parent_name: str, inherited_ft, inherited_ff: int, depth: int) -> None:
        nonlocal cycle_reported
        if not isinstance(node, pikepdf.Dictionary):
            return
        key = node.objgen
        if depth > _MAX_TREE_DEPTH or (key[0] and key in seen):
            if not cycle_reported:
                warnings.append(_warning("field_tree_invalid",
                                         "The form field tree contains a cycle or is too deep."))
                cycle_reported = True
            return
        if key[0]:
            seen.add(key)
        partial = _text(node.get("/T"))
        full_name = ".".join(p for p in (parent_name, partial) if p) if partial is not None else parent_name
        ft = node.get("/FT", inherited_ft)
        ff = int(node.get("/Ff", inherited_ff))
        kids = list(node.get("/Kids", []))
        field_kids = [k for k in kids if isinstance(k, pikepdf.Dictionary) and "/T" in k]
        if field_kids:
            for kid in field_kids:
                walk(kid, full_name, ft, ff, depth + 1)
            return
        terminal = _Field(node, full_name, _name(ft) if ft is not None else None, ff)
        if node.get("/Subtype") == pikepdf.Name.Widget:
            terminal.widgets.append(node)
        terminal.widgets.extend(k for k in kids if isinstance(k, pikepdf.Dictionary))
        fields.append(terminal)

    for node in acroform.get("/Fields", []):
        walk(node, "", None, 0, 0)
    return fields


def _on_state_keys(widget) -> list[str]:
    """The widget's "on" appearance states as pikepdf returns the keys (raw; see `names.name_bytes`)."""
    ap = widget.get("/AP")
    normal = ap.get("/N") if isinstance(ap, pikepdf.Dictionary) else None
    if not isinstance(normal, pikepdf.Dictionary):
        return []
    return [k for k in normal.keys() if names_mod.name_bytes(k) != b"Off"]


def on_states(widget) -> list[str]:
    """The widget's "on" states as contract values (`options[].value`, `export_value`); shared with fill."""
    return [_name(k) for k in _on_state_keys(widget)]


def _choice_options(field_obj) -> list[dict]:
    options = []
    for entry in field_obj.get("/Opt", []):
        if isinstance(entry, pikepdf.Array) and len(entry) >= 2:
            value, label = _text(entry[0]), _text(entry[1])
        else:
            value = label = _text(entry)
        if value is not None:
            options.append({"value": value, "label": label if label is not None else value})
    return options


def _classify(f: _Field) -> dict | None:
    """Contract type and type-specific attributes, or None for unsupported fields."""
    if f.field_type == "Tx":
        attrs = {"type": "text", "multiline": bool(f.flags & _FF_TX_MULTILINE)}
        if f.flags & _FF_TX_PASSWORD:
            attrs["password_field"] = True
        max_len = f.obj.get("/MaxLen")
        if max_len is not None:
            attrs["max_length"] = int(max_len)
        return attrs
    if f.field_type == "Btn":
        if f.flags & _FF_BTN_PUSHBUTTON:
            return None
        return {"type": "radio" if f.flags & _FF_BTN_RADIO else "checkbox"}
    if f.field_type == "Ch":
        return {
            "type": "choice",
            "choice_style": "combo" if f.flags & _FF_CH_COMBO else "list",
            "editable": bool(f.flags & _FF_CH_EDIT),
            "multi_select": bool(f.flags & _FF_CH_MULTISELECT),
            "options": _choice_options(f.obj),
        }
    if f.field_type == "Sig":
        return {"type": "signature", "signed": f.obj.get("/V") is not None}
    return None


def _button_options(f: _Field) -> list[dict]:
    """Export values of a checkbox/radio field; /Opt supplies labels when present."""
    labels = [_text(o) for o in f.obj.get("/Opt", [])]
    options, seen = [], set()
    for widget in f.widgets:
        for key in _on_state_keys(widget):
            state = _name(key)
            if state in seen:
                continue
            seen.add(state)
            label = names_mod.name_label(key)
            if labels and state.isdigit() and int(state) < len(labels) and labels[int(state)]:
                label = labels[int(state)]
            options.append({"value": state, "label": label})
    return options


@dataclass(frozen=True)
class NativeWidget:
    """One native widget as reported by inspect (`item`) plus its PDF objects (used by forms/fill)."""

    item: dict
    widget: pikepdf.Dictionary
    field: pikepdf.Dictionary
    siblings: tuple[pikepdf.Dictionary, ...]  # every widget of the same terminal field (incl. this one)


def native_widgets(
    pdf: pikepdf.Pdf, boxes: list[PageBox], warnings: list[dict], max_fields: int
) -> list[NativeWidget]:
    """Native widgets in reading order with the ids/refs/positions of the inspect contract."""
    acroform = pdf.Root.get("/AcroForm")
    if not isinstance(acroform, pikepdf.Dictionary):
        return []

    # Widget -> page via the pages' /Annots (more reliable than a widget's /P).
    page_by_widget: dict[tuple[int, int], int] = {}
    for number, page in enumerate(pdf.pages, start=1):
        for annot in page.obj.get("/Annots", []):
            if isinstance(annot, pikepdf.Dictionary) and annot.objgen[0]:
                page_by_widget.setdefault(annot.objgen, number)
    page_by_obj = {page.obj.objgen: n for n, page in enumerate(pdf.pages, start=1)}

    entries: list[tuple[_Field, dict, _Widget, int, int]] = []
    counts = {"push_buttons": 0, "unsupported": 0, "unnamed": 0, "without_page": 0, "outside": 0}
    for f in _collect_fields(acroform, warnings):
        attrs = _classify(f)
        if attrs is None:
            counts["push_buttons" if f.field_type == "Btn" else "unsupported"] += 1
            continue
        if not f.full_name:
            counts["unnamed"] += 1
            continue
        if attrs["type"] in ("checkbox", "radio"):
            attrs["options"] = _button_options(f)
        located: list[_Widget] = []
        for widget in f.widgets:
            page = page_by_widget.get(widget.objgen)
            if page is None and isinstance(widget.get("/P"), pikepdf.Dictionary):
                page = page_by_obj.get(widget.P.objgen)
            rect = widget.get("/Rect")
            if page is None or rect is None or len(rect) != 4:
                counts["without_page"] += 1
                continue
            box = boxes[page - 1]
            visible, clipped = box.clip(box.to_visible(*(float(v) for v in rect)))
            if visible is None:
                counts["outside"] += 1
                continue
            located.append(_Widget(widget, page, visible))
            if clipped:
                warnings.append(_warning("widget_clipped", "A field widget extends beyond the visible page "
                                         "and was clipped to it.", field_ref=_ref(f.obj) or _ref(widget)))
        for index, w in enumerate(located):
            entries.append((f, attrs, w, index, len(located)))
        if len(entries) > max_fields:
            raise DocPipeError("limit_exceeded", "The PDF has more form fields than allowed.")

    messages = {
        "push_buttons": ("push_buttons_ignored", "Push buttons cannot be filled and were ignored."),
        "unsupported": ("unsupported_fields_ignored", "Fields of an unknown type were ignored."),
        "unnamed": ("unnamed_fields_ignored", "Fields without a name cannot be filled and were ignored."),
        "without_page": ("widgets_without_page_ignored", "Widgets not placed on any page were ignored."),
        "outside": ("widgets_outside_page_ignored",
                    "Widgets entirely outside the visible page were ignored."),
    }
    for key, count in counts.items():
        if count:
            code, message = messages[key]
            warnings.append(_warning(code, message, count=count))

    entries.sort(key=lambda e: (e[2].page, round(e[2].rect.y, 1), round(e[2].rect.x, 1)))
    result: list[NativeWidget] = []
    for number, (f, attrs, w, index, count) in enumerate(entries, start=1):
        flags = int(w.obj.get("/F", 0))
        item = {
            "id": f"field_{number:03d}",
            "name": _text(f.obj.get("/TU")) or f.full_name,
            "input_name": f.full_name,
            "native_ref": _ref(w.obj) or f"page{w.page}.widget{number}",
            "field_ref": _ref(f.obj),
            "widget_index": index,
            "widget_count": count,
            "page": w.page,
            "position": w.rect.as_dict(),
            "source": "native",
            "required": bool(f.flags & _FF_REQUIRED),
            "read_only": bool(f.flags & _FF_READ_ONLY),
            "hidden": bool(flags & (_F_HIDDEN | _F_NOVIEW)),
            "options": [],
            **attrs,
        }
        if attrs["type"] in ("checkbox", "radio"):
            states = on_states(w.obj)
            item["export_value"] = states[0] if states else None
        result.append(NativeWidget(item, w.obj, f.obj, tuple(f.widgets)))
    return result


def _inspect_fields(
    pdf: pikepdf.Pdf, boxes: list[PageBox], warnings: list[dict], max_fields: int
) -> list[dict]:
    return [n.item for n in native_widgets(pdf, boxes, warnings, max_fields)]


def _plain_copy(pdf: pikepdf.Pdf) -> bytes:
    """Unencrypted in-memory copy for layout analysis; the upload itself is never modified."""
    out = io.BytesIO()
    pdf.save(out, encryption=False)
    return out.getvalue()


def _apply_ocr(pdf, boxes, layouts, sources, textless, runner, max_ocr_pages, warnings) -> dict:
    """OCR the text-less pages; updates layouts/sources in place. Returns the `ocr` block."""
    to_ocr = textless[:max_ocr_pages]
    info: dict = {"status": "applied", "pages": []}
    if len(textless) > len(to_ocr):
        info["status"] = "partial"
        warnings.append(_warning("ocr_page_limit", "More pages need OCR than allowed; the remaining pages "
                                 "were not processed.", count=len(textless) - len(to_ocr)))
    try:
        result = ocr_mod.run(pdf, boxes, to_ocr, runner)
    except DocPipeError as exc:
        warnings.append(_warning("ocr_failed", "OCR could not be performed; pages without a text layer "
                                 "are returned without text.", reason=exc.code))
        return {"status": "failed", "pages": [], "reason": exc.code}
    ocr_layouts = layout_mod.extract(result.pdf, {pos: page.post for pos, page in result.pages.items()})
    for pos, page in result.pages.items():
        index = page.page - 1
        found = ocr_layouts[pos]
        has_text = bool(found.text.strip())
        info["pages"].append({"page": page.page, "geometry": page.geometry, "text_found": has_text})
        if not has_text:
            warnings.append(_warning("ocr_no_text", "OCR found no text on the page.", page=page.page))
            continue
        layouts[index], sources[index] = found, "ocr"
        if page.geometry == "mismatch":
            warnings.append(_warning("ocr_geometry_mismatch", "The OCR output does not match the page "
                                     "geometry; its text is returned without positions.", page=page.page))
    return info


def _detected_fields(layouts, boxes, fields, warnings) -> list[dict]:
    native_by_page: dict[int, list[VisibleRect]] = {}
    for f in fields:
        p = f["position"]
        native_by_page.setdefault(f["page"], []).append(VisibleRect(p["x"], p["y"], p["width"], p["height"]))
    result = []
    for index, layout in sorted(layouts.items()):
        number = index + 1
        box = boxes[index]
        candidates, truncated = detect_mod.detect(number, layout, box.width, box.height,
                                                  native_by_page.get(number, []))
        if truncated:
            warnings.append(_warning("detection_limit", "The page has more field proposals than allowed; "
                                     "only the most likely ones are returned.", page=number))
        for c in candidates:
            result.append({
                "id": f"detected_{len(result) + 1:03d}",
                "name": c.label_text,
                "input_name": None,
                "native_ref": None,
                "field_ref": None,
                "page": number,
                "position": c.rect.as_dict(),
                "source": "detected",
                "type": c.type,
                "detection": c.detection,
                "confidence": c.confidence,
                "label": ({"text": c.label.text, "position": c.label.rect.as_dict()}
                          if c.label is not None else None),
                "multiline": c.multiline,
                "options": [],
            })
    if result:
        low = sum(1 for f in result if f["confidence"] < detect_mod.LOW_CONFIDENCE)
        warnings.append(_warning("fields_detected", "Field proposals were detected from the page layout; "
                                 "they must be reviewed and confirmed before use.", count=len(result),
                                 low_confidence_count=low))
    return result


def inspect_pdf(
    data: bytes,
    password: str | None = None,
    *,
    max_pages: int,
    max_fields: int,
    ocr: ocr_mod.OcrRunner | None = None,
    ocr_skipped: str = "disabled",
    max_ocr_pages: int = 20,
    detect: bool = True,
) -> dict:
    """Inspect one PDF. Raises DocPipeError for documents that cannot be inspected.

    `ocr` runs OCR on pages without a text layer (None: not available; then
    `ocr_skipped` - "disabled" or "off" - is reported). `detect` adds layout
    proposals for static fields.
    """
    sha256 = hashlib.sha256(data).hexdigest()
    pdf = _open(data, password)
    try:
        warnings: list[dict] = []
        if pdf.get_warnings():
            warnings.append(_warning("pdf_structure_repaired",
                                     "The PDF structure was damaged and has been read in repaired form."))
        page_count = len(pdf.pages)
        if page_count == 0:
            raise DocPipeError("invalid_file", "The PDF has no pages.")
        if page_count > max_pages:
            raise DocPipeError("limit_exceeded", "The PDF has more pages than allowed.")

        security = _security(pdf, bool(password) and pdf.is_encrypted and _needs_open_password(data))
        acroform = pdf.Root.get("/AcroForm")
        has_xfa = isinstance(acroform, pikepdf.Dictionary) and "/XFA" in acroform
        needs_rendering = bool(pdf.Root.get("/NeedsRendering", False))
        boxes = [PageBox.from_page(page.obj) for page in pdf.pages]
        for number, box in enumerate(boxes, start=1):
            if not box.rotation_valid:
                warnings.append(_warning("invalid_page_rotation",
                                         "The page rotation is not a multiple of 90 degrees; 0 was assumed.",
                                         page=number))

        fields = _inspect_fields(pdf, boxes, warnings, max_fields)
        if needs_rendering or (has_xfa and not fields):
            raise DocPipeError("xfa_unsupported", "Dynamic XFA forms are not supported.")
        if has_xfa:
            warnings.append(_warning("xfa_present", "The PDF also contains an XFA form; only its AcroForm "
                                     "fields are reported and will be used."))

        signed = any(f["type"] == "signature" and f.get("signed") for f in fields)
        perms = pdf.Root.get("/Perms")
        certified = isinstance(perms, pikepdf.Dictionary) and "/DocMDP" in perms
        security.update({"signed": signed, "certified": certified, "xfa": has_xfa})
        if signed:
            warnings.append(_warning("signed_document", "The PDF carries a digital signature; filling it "
                                     "would invalidate the signature."))
        if certified:
            warnings.append(_warning("certified_document", "The PDF is certified (DocMDP); changes may be "
                                     "restricted by the certification."))
        permissions = security["permissions"]
        if permissions is not None and not permissions["fill_forms"]:
            warnings.append(_warning("form_filling_not_permitted",
                                     "The PDF's permissions do not allow filling in form fields."))

        layouts: dict[int, layout_mod.PageLayout] = {}
        sources = ["none"] * page_count
        ocr_info: dict = {"status": "not_permitted", "pages": []}
        detected: list[dict] = []
        text_allowed = permissions is None or permissions["extract_text"]
        if not text_allowed:
            warnings.append(_warning("text_extraction_not_permitted",
                                     "The PDF's permissions do not allow text extraction; "
                                     "no text, OCR or field proposals are returned."))
        else:
            try:
                posts = {i: layout_mod.identity for i in range(page_count)}
                layouts = layout_mod.extract(_plain_copy(pdf), posts)
            except Exception:  # noqa: BLE001 - layout analysis must not fail the structural result
                layouts = {}
                warnings.append(_warning("text_extraction_failed", "The page text could not be extracted."))
            textless = [i for i in range(page_count) if i in layouts and not layouts[i].text.strip()]
            for i in range(page_count):
                if i in layouts and layouts[i].text.strip():
                    sources[i] = "embedded"
            if not layouts:
                ocr_info = {"status": "failed", "pages": [], "reason": "text_extraction_failed"}
            elif not textless:
                ocr_info = {"status": "not_needed", "pages": []}
            elif ocr is None:
                ocr_info = {"status": ocr_skipped, "pages": []}
            else:
                ocr_info = _apply_ocr(pdf, boxes, layouts, sources, textless, ocr, max_ocr_pages, warnings)
            processed = {p["page"] for p in ocr_info["pages"]}
            for i in textless:
                if sources[i] == "none" and i + 1 not in processed:
                    warnings.append(_warning("no_text_layer", "The page has no extractable text.",
                                             page=i + 1))
            if detect and layouts:
                detected = _detected_fields(layouts, boxes, fields, warnings)

        pages = []
        for index, box in enumerate(boxes):
            found = layouts.get(index)
            has_text = found is not None and sources[index] != "none"
            pages.append({
                "page": index + 1,
                "width": round(box.width, 2),
                "height": round(box.height, 2),
                "rotation": box.rotation,
                "text_source": sources[index],
                "text": found.text if has_text else "",
                "text_blocks": found.blocks() if has_text else [],
            })

        result = {
            "schema_version": SCHEMA_VERSION,
            "sha256": sha256,
            "page_count": page_count,
            "text": "\n\n".join(p["text"] for p in pages if p["text"]),
            "pages": pages,
            "fields": fields + detected,
            "ocr": ocr_info,
            "detection": {"enabled": bool(detect and text_allowed), "count": len(detected)},
            "security": security,
            "warnings": warnings,
        }
        # Last line of defence: the JSON response must never contain a lone surrogate (it cannot be encoded
        # and used to end in HTTP 500). Names are already reversible text; any other text is repaired here.
        replaced = [0]
        result = names_mod.json_safe(result, replaced)
        if replaced[0]:
            result["warnings"].append(_warning("text_unencodable_replaced", "Text that cannot be represented "
                                               "as Unicode was replaced by U+FFFD.", count=replaced[0]))
        return result
    finally:
        pdf.close()
