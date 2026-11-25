"""Phase 2.2: POST /api/v1/documents/forms/fill.

Visual tests render the OUTPUT with PDFium (pypdfium2, dev dependency) the way a
viewer shows it - CropBox, page rotation applied, origin top-left - and measure
where ink actually lands. 1 rendered pixel = 1 / SCALE pt, so ink boxes compare
directly with the contract's positions.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging

import jsonschema
import pikepdf
import pypdfium2 as pdfium
import pytest
from pdfminer.high_level import extract_text
from PIL import Image

from system.errors import DocPipeError
from system.forms import render as rnd
from system.forms.fill import fill_pdf, load_images, parse_manifest
from system.forms.inspect import inspect_pdf
from tests.conftest import VALID_KEY, auth_headers
from tests.forms_fixtures import (
    FIXTURE_DIR,
    OWNER_PASSWORD,
    ROTATED_FIELD_VISIBLE,
    USER_PASSWORD,
)
from tests.test_forms_inspect import FORMS_KEY, forms_client  # noqa: F401 - fixture

FILL_URL = "/api/v1/documents/forms/fill"
LIMITS = {"max_pages": 200, "max_fields": 2000}
SCALE = 3.0


def fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def fields_of(name: str, password: str | None = None) -> dict[str, dict]:
    data = inspect_pdf(fixture(name), password, **LIMITS)
    result = {}
    for f in data["fields"]:
        key = f["input_name"] if f["type"] != "radio" else f"{f['input_name']}/{f['export_value']}"
        result.setdefault(key, f)
    return result


def native(field: dict, value, *, id_: str | None = None, type_: str | None = None, **extra) -> dict:
    kind = (
        type_ or {"text": "text", "choice": "choice", "checkbox": "checkbox", "radio": "radio"}[field["type"]]
    )
    return {
        "id": id_ or field["id"],
        "mode": "native",
        "type": kind,
        "native_ref": field["native_ref"],
        "value": value,
        **extra,
    }


def overlay(id_: str, page: int, x, y, w, h, value=None, type_: str = "text", **extra) -> dict:
    return {
        "id": id_,
        "mode": "overlay",
        "type": type_,
        "page": page,
        "position": {"x": x, "y": y, "width": w, "height": h},
        "value": value,
        **extra,
    }


def manifest(name: str, fields: list[dict], **output) -> dict:
    return {
        "schema_version": "1",
        "sha256": hashlib.sha256(fixture(name)).hexdigest(),
        "fields": fields,
        "output": output or {"flatten": True},
    }


def fill(
    name: str, fields: list[dict], images: dict[str, bytes] | None = None, password=None, **output
) -> bytes:
    m = parse_manifest(json.dumps(manifest(name, fields, **output)))
    return fill_pdf(fixture(name), m, load_images(images or {}), password, **LIMITS).pdf


def fill_error(name: str, fields: list[dict], images=None, password=None, **output) -> DocPipeError:
    with pytest.raises(DocPipeError) as exc:
        fill(name, fields, images, password, **output)
    return exc.value


def render(pdf: bytes, page: int = 1, password: str | None = None) -> Image.Image:
    doc = pdfium.PdfDocument(pdf, password=password)
    return doc[page - 1].render(scale=SCALE).to_pil().convert("RGB")


def ink(image: Image.Image, box: dict | None = None, threshold: int = 160) -> dict | None:
    """Bounding box (points, top-left) of dark pixels, optionally only inside `box` (points)."""
    gray = image.convert("L")
    if box is not None:
        left, top = int(box["x"] * SCALE), int(box["y"] * SCALE)
        right, bottom = int((box["x"] + box["width"]) * SCALE), int((box["y"] + box["height"]) * SCALE)
        gray = gray.crop((left, top, right, bottom))
    else:
        left = top = 0
    mask = gray.point(lambda v: 255 if v < threshold else 0)
    bbox = mask.getbbox()
    if bbox is None:
        return None
    x0, y0, x1, y1 = bbox
    return {
        "x": (x0 + left) / SCALE,
        "y": (y0 + top) / SCALE,
        "width": (x1 - x0) / SCALE,
        "height": (y1 - y0) / SCALE,
    }


def inside(inner: dict, outer: dict, tolerance: float = 0.7) -> bool:
    return (
        inner["x"] >= outer["x"] - tolerance
        and inner["y"] >= outer["y"] - tolerance
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + tolerance
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + tolerance
    )


def png(width: int, height: int, color=(200, 0, 0, 255), transparent_border: int = 0) -> bytes:
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    inner = Image.new("RGBA", (width - 2 * transparent_border, height - 2 * transparent_border), color)
    image.paste(inner, (transparent_border, transparent_border))
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


# --- native fields, Unicode, styles -------------------------------------------------------------------


def test_native_fields_are_filled_and_the_output_is_flat():
    f = fields_of("basic-form.pdf")
    out = fill(
        "basic-form.pdf",
        [
            native(f["vorname"], "Zoë Müller-Łęcka Жанна"),
            native(f["bemerkung"], "Erste Zeile\nZweite Zeile"),
            native(f["land"], "AT"),
        ],
    )
    pdf = pikepdf.open(io.BytesIO(out))
    assert "/AcroForm" not in pdf.Root
    assert not any(
        a.get("/Subtype") == pikepdf.Name.Widget for p in pdf.pages for a in p.obj.get("/Annots", [])
    )
    text = extract_text(io.BytesIO(out))
    assert "Zoë Müller-Łęcka Жанна" in text and "Erste Zeile" in text and "Zweite Zeile" in text
    assert "Österreich" in text and "AT" not in text.replace("Österreich", "")  # label, not export value


def test_text_is_drawn_inside_the_field():
    f = fields_of("basic-form.pdf")
    image = render(fill("basic-form.pdf", [native(f["vorname"], "Mustermann")]))
    drawn = ink(image, f["vorname"]["position"])
    assert drawn is not None and inside(drawn, f["vorname"]["position"])
    assert drawn["x"] == pytest.approx(f["vorname"]["position"]["x"] + rnd.PAD_X, abs=1.0)  # left aligned


@pytest.mark.parametrize("align", ["left", "center", "right"])
def test_alignment(align):
    f = fields_of("basic-form.pdf")
    field = f["vorname"]["position"]
    drawn = ink(render(fill("basic-form.pdf", [native(f["vorname"], "Abc", style={"align": align})])), field)
    left_gap = drawn["x"] - field["x"]
    right_gap = field["x"] + field["width"] - (drawn["x"] + drawn["width"])
    if align == "left":
        assert left_gap < 4 < right_gap
    elif align == "right":
        assert right_gap < 4 < left_gap
    else:
        assert abs(left_gap - right_gap) < 1.5


def test_color_font_family_weight_and_style_are_applied():
    f = fields_of("basic-form.pdf")
    out = fill(
        "basic-form.pdf",
        [
            native(
                f["vorname"],
                "Farbe",
                style={
                    "color": "#1030a0",
                    "font_family": "serif",
                    "font_weight": "bold",
                    "font_style": "italic",
                },
            )
        ],
    )
    image = render(out)
    box = ink(image, f["vorname"]["position"])
    pixels = [
        image.getpixel((int(x * SCALE), int(y * SCALE)))
        for x in range(int(box["x"]), int(box["x"] + box["width"]))
        for y in range(int(box["y"]), int(box["y"] + box["height"]))
    ]
    darkest = min(pixels, key=sum)
    assert darkest[2] > darkest[0] + 60  # blue, not black
    fonts = {
        str(font.BaseFont)
        for page in pikepdf.open(io.BytesIO(out)).pages
        for xobj in page.obj.Resources.XObject.values()
        for font in xobj.get("/Resources", {}).get("/Font", {}).values()
    }
    assert any("LiberationSerif-BoldItalic" in name for name in fonts)


def test_auto_fit_shrinks_and_never_cuts():
    # an overlay field of the same size as "vorname" (which has /MaxLen 40 -> invalid_value first)
    box = {"x": 300, "y": 400, "width": 200, "height": 20}
    long = "Ein sehr langer Familienname mit vielen Teilen"
    err = fill_error("basic-form.pdf", [overlay("n", 1, *box.values(), long, style={"font_size": 12})])
    assert err.code == "text_overflow" and err.status_code == 422
    assert err.details == {"field_id": "n"} and long not in err.message
    out = fill(
        "basic-form.pdf",
        [overlay("n", 1, *box.values(), long, style={"font_size": 12, "auto_fit": True, "min_font_size": 6})],
    )
    assert long in extract_text(io.BytesIO(out)).replace("\n", "")
    assert inside(ink(render(out), box), box)
    err = fill_error("basic-form.pdf", [overlay("n", 1, *box.values(), long * 3, style={"auto_fit": True})])
    assert err.code == "text_overflow"
    err = fill_error("basic-form.pdf", [native(fields_of("basic-form.pdf")["vorname"], long)])
    assert err.code == "invalid_value" and err.details["reason"] == "too_long"  # /MaxLen checked first


def test_multiline_wraps_and_overflows_with_an_error():
    f = fields_of("basic-form.pdf")
    box = f["bemerkung"]["position"]  # 400 x 80, multiline flag
    text = "Wort " * 60
    out = fill("basic-form.pdf", [native(f["bemerkung"], text.strip())])
    drawn = ink(render(out), box)
    assert inside(drawn, box) and drawn["height"] > 30  # several lines
    err = fill_error("basic-form.pdf", [native(f["bemerkung"], "Zeile\n" * 12)])
    assert err.code == "text_overflow"


def test_single_line_field_rejects_line_breaks_unless_multiline_is_requested():
    f = fields_of("basic-form.pdf")
    assert fill_error("basic-form.pdf", [native(f["vorname"], "a\nb")]).code == "text_overflow"
    pos = f["vorname"]["position"]
    err = fill_error("basic-form.pdf", [native(f["vorname"], "a\nb", style={"multiline": True})])
    assert err.code == "text_overflow" and pos["height"] == 20  # two 10 pt lines need > 20 pt
    fill("basic-form.pdf", [native(f["vorname"], "a\nb", style={"multiline": True, "font_size": 7})])


def test_unsupported_characters_are_an_error_without_the_value():
    f = fields_of("basic-form.pdf")
    err = fill_error("basic-form.pdf", [native(f["vorname"], "Name 漢字 \U0001f600")])
    assert err.code == "unsupported_characters" and err.details == {
        "field_id": f["vorname"]["id"],
        "count": 3,
    }
    assert "漢" not in err.message


def test_layout_unit_auto_fit_size():
    style = rnd.TextStyle(size=12, auto_fit=True, min_size=6)
    layout = rnd.layout_text("Breiter Text in schmaler Box", 80, 20, style, multiline=False)
    assert 6 <= layout.size < 12 and layout.lines == ("Breiter Text in schmaler Box",)
    with pytest.raises(rnd.TextOverflow):
        rnd.layout_text("x" * 200, 80, 20, style, multiline=False)


# --- checkboxes and radio buttons ------------------------------------------------------------------


def test_checkbox_and_radio_use_the_templates_appearance_states():
    f = fields_of("basic-form.pdf")
    out = fill("basic-form.pdf", [native(f["einverstanden"], True), native(f["anrede/herr"], "herr")])
    image = render(out)
    assert ink(image, f["einverstanden"]["position"]) is not None
    assert ink(image, f["anrede/herr"]["position"]) is not None
    assert ink(image, f["anrede/frau"]["position"]) is None  # only the chosen button
    empty = render(fill("basic-form.pdf", [native(f["einverstanden"], False)]))
    assert ink(empty, f["einverstanden"]["position"]) is None


def test_radio_value_can_be_sent_via_any_widget_of_the_group():
    f = fields_of("basic-form.pdf")
    image = render(fill("basic-form.pdf", [native(f["anrede/frau"], "herr")]))
    assert ink(image, f["anrede/herr"]["position"]) is not None
    assert ink(image, f["anrede/frau"]["position"]) is None


def test_checkbox_export_value_and_invalid_options():
    f = fields_of("basic-form.pdf")
    fill("basic-form.pdf", [native(f["einverstanden"], "Ja")])
    err = fill_error("basic-form.pdf", [native(f["einverstanden"], "Yes")])
    assert err.code == "invalid_value" and err.details["reason"] == "option"
    assert fill_error("basic-form.pdf", [native(f["anrede/herr"], "divers")]).details["reason"] == "option"
    assert fill_error("basic-form.pdf", [native(f["land"], "FR")]).details["reason"] == "option"


def test_checkbox_without_appearance_gets_a_vector_mark():
    f = fields_of("appearance-form.pdf")
    box = f["ok"]["position"]
    assert ink(render(fill("appearance-form.pdf", [native(f["ok"], True)])), box) is not None
    assert ink(render(fill("appearance-form.pdf", [native(f["ok"], False)])), box) is None


def test_old_appearance_text_is_replaced_but_the_border_is_kept():
    f = fields_of("appearance-form.pdf")
    out = fill("appearance-form.pdf", [native(f["name"], "NEU")])
    text = extract_text(io.BytesIO(out))
    assert "NEU" in text and "ALT" not in text
    box = f["name"]["position"]
    image = render(out)
    border = ink(
        image, {"x": box["x"] - 1, "y": box["y"] - 1, "width": box["width"] + 2, "height": 2}, threshold=220
    )
    assert border is not None and border["width"] > box["width"] * 0.9  # the grey top border line
    # unlisted fields keep their template appearance (old value) - nothing is invented
    untouched = extract_text(io.BytesIO(fill("appearance-form.pdf", [native(f["ok"], True)])))
    assert "ALT" in untouched


# --- geometry: rotation, CropBox, multi-widget -------------------------------------------------------


@pytest.mark.parametrize("rotation, page", [(0, 1), (90, 2), (180, 3), (270, 4)])
def test_upright_text_on_rotated_pages(rotation, page):
    f = fields_of("rotated-fields.pdf")
    field = f[f"feld_r{rotation}"]
    assert field["position"] == ROTATED_FIELD_VISIBLE
    out = fill("rotated-fields.pdf", [native(field, "Aufrecht lesbar", style={"font_size": 12})])
    drawn = ink(render(out, page), ROTATED_FIELD_VISIBLE)
    assert drawn is not None and inside(drawn, ROTATED_FIELD_VISIBLE)
    assert drawn["width"] > 3 * drawn["height"]  # horizontal text line on the displayed page
    everywhere = ink(render(out, page))
    assert inside(everywhere, ROTATED_FIELD_VISIBLE)  # nothing drawn anywhere else


@pytest.mark.parametrize("rotation, page", [(0, 1), (90, 2), (180, 3), (270, 4)])
def test_overlay_and_image_on_rotated_pages(rotation, page):
    target = {"x": 300, "y": 400, "width": 120, "height": 60}
    out = fill(
        "rotated-fields.pdf",
        [
            overlay("t", page, 100, 300, 200, 20, "Overlay"),
            overlay(
                "img",
                page,
                target["x"],
                target["y"],
                target["width"],
                target["height"],
                type_="stamp_image",
                image_ref="stamp.png",
            ),
        ],
        images={"stamp.png": png(200, 100)},
    )
    image = render(out, page)
    assert inside(
        ink(image, {"x": 100, "y": 300, "width": 200, "height": 20}),
        {"x": 100, "y": 300, "width": 200, "height": 20},
    )
    stamp = ink(image, target, threshold=250)
    # 200 x 100 px into 120 x 60 pt: exact fit, ratio 2:1
    assert stamp["width"] == pytest.approx(120, abs=1) and stamp["height"] == pytest.approx(60, abs=1)


def test_cropbox_offset_page():
    f = fields_of("cropbox-offset.pdf")
    field = f["name"]
    out = fill("cropbox-offset.pdf", [native(field, "Versetzt"), overlay("o", 1, 300, 400, 150, 20, "Frei")])
    image = render(out)
    assert inside(ink(image, field["position"]), field["position"])
    assert inside(
        ink(image, {"x": 300, "y": 400, "width": 150, "height": 20}),
        {"x": 300, "y": 400, "width": 150, "height": 20},
    )


def test_value_of_a_multi_widget_field_appears_on_every_widget():
    f = inspect_pdf(fixture("multi-widget-hierarchy.pdf"), None, **LIMITS)["fields"]
    first, second = [x for x in f if x["input_name"] == "person.name"]
    out = fill("multi-widget-hierarchy.pdf", [native(first, "Li", style={"font_size": 8})])
    assert "Li" in extract_text(io.BytesIO(out), page_numbers=[0])
    assert "Li" in extract_text(io.BytesIO(out), page_numbers=[1])
    for widget in (first, second):
        assert ink(render(out, widget["page"]), widget["position"]) is not None


def test_widgets_of_one_field_must_not_get_different_values():
    f = inspect_pdf(fixture("multi-widget-hierarchy.pdf"), None, **LIMITS)["fields"]
    first, second = [x for x in f if x["input_name"] == "person.name"]
    err = fill_error("multi-widget-hierarchy.pdf", [native(first, "A"), native(second, "B")])
    assert err.code == "field_conflict" and err.details["reason"] == "inconsistent_value"
    fill("multi-widget-hierarchy.pdf", [native(first, "A"), native(second, "A")])


def test_hidden_widgets_stay_invisible():
    f = fields_of("multi-widget-hierarchy.pdf")
    hidden = f["intern"]
    assert hidden["hidden"] is True
    out = fill("multi-widget-hierarchy.pdf", [native(hidden, "Unsichtbar")])
    assert "Unsichtbar" not in extract_text(io.BytesIO(out))
    assert ink(render(out, hidden["page"]), hidden["position"]) is None


# --- images --------------------------------------------------------------------------------------------


def test_image_is_fitted_proportionally_and_centred_with_transparency():
    # an empty area of rotated-fields.pdf page 1 (its only content is the field at y 80)
    target = {"x": 100, "y": 300, "width": 150, "height": 150}
    out = fill(
        "rotated-fields.pdf",
        [overlay("sig", 1, 100, 300, 150, 150, type_="signature_image", image_ref="sig.png")],
        images={"sig.png": png(200, 100, transparent_border=20)},
    )
    image = render(out)
    drawn = ink(image, target, threshold=250)
    # 200 x 100 px -> 150 x 75 pt (scale 0.75), centred vertically: y 337.5 .. 412.5. The 20 px
    # transparent border (15 pt) shows the page below, so the red part is 120 x 45 pt at (115, 352.5).
    assert drawn["width"] == pytest.approx(120, abs=1) and drawn["height"] == pytest.approx(45, abs=1)
    assert drawn["x"] == pytest.approx(115, abs=1) and drawn["y"] == pytest.approx(352.5, abs=1)
    corner = image.getpixel((int(105 * SCALE), int(345 * SCALE)))  # inside the image, transparent border
    assert min(corner) > 240


@pytest.mark.parametrize(
    "data, reason",
    [
        (b"GIF89a....", "not_png"),
        (b"\x89PNG\r\n\x1a\n" + b"broken", "broken"),
    ],
)
def test_invalid_images(data, reason):
    err = fill_error(
        "static-form.pdf",
        [overlay("s", 1, 10, 10, 50, 50, type_="stamp_image", image_ref="a.png")],
        images={"a.png": data},
    )
    assert err.code == "invalid_image" and err.details == {"image_ref": "a.png"}


def test_image_ref_must_exist_and_images_need_overlay_mode():
    err = fill_error(
        "static-form.pdf", [overlay("s", 1, 10, 10, 50, 50, type_="stamp_image", image_ref="missing.png")]
    )
    assert err.code == "invalid_manifest" and err.details == {"field_id": "s"}
    f = fields_of("basic-form.pdf")
    err = fill_error("basic-form.pdf", [native(f["vorname"], None, type_="stamp_image")])
    assert err.code == "invalid_manifest"


# --- manifest validation ------------------------------------------------------------------------------


def test_hash_mismatch():
    m = manifest("basic-form.pdf", [])
    m["sha256"] = "0" * 64
    with pytest.raises(DocPipeError) as exc:
        fill_pdf(fixture("basic-form.pdf"), parse_manifest(json.dumps(m)), {}, None, **LIMITS)
    assert exc.value.code == "hash_mismatch" and exc.value.status_code == 409


@pytest.mark.parametrize(
    "mutate, path",
    [
        (lambda m: m.update(extra=1), "extra"),
        (
            lambda m: m["fields"].append(
                {
                    "id": "x",
                    "mode": "overlay",
                    "type": "text",
                    "page": 1,
                    "position": {"x": 1, "y": 1, "width": 10, "height": 10},
                    "style": {"color": "red"},
                }
            ),
            "fields.0.style.color",
        ),
        (lambda m: m["fields"].append({"id": "bad id!", "mode": "overlay", "type": "text"}), "fields.0.id"),
        (lambda m: m.update(output={"flatten": False}), "output.flatten"),
        (lambda m: m.update(schema_version="2"), "schema_version"),
    ],
)
def test_invalid_manifest_reports_the_path_only(mutate, path):
    m = manifest("basic-form.pdf", [])
    mutate(m)
    with pytest.raises(DocPipeError) as exc:
        parse_manifest(json.dumps(m))
    assert exc.value.code == "invalid_manifest" and exc.value.details == {"path": path}


def test_native_reference_and_consistency_checks():
    f = fields_of("basic-form.pdf")
    v = f["vorname"]
    cases = [
        ([{**native(v, "x"), "native_ref": "999.0"}], "unknown_field", None),
        ([native(v, "x"), {**native(v, "y"), "id": "dup"}], "field_conflict", "duplicate_native_ref"),
        ([native(v, True, type_="checkbox")], "field_conflict", "type_mismatch"),
        ([{**native(v, "x"), "input_name": "nachname"}], "field_conflict", "input_name_mismatch"),
        ([{**native(v, "x"), "page": 2}], "field_conflict", "geometry_mismatch"),
        (
            [{**native(v, "x"), "position": {**v["position"], "x": v["position"]["x"] + 5}}],
            "field_conflict",
            "geometry_mismatch",
        ),
        ([native(v, "")], "invalid_value", "missing_value"),  # /Ff required
        ([native(v, "x" * 41)], "invalid_value", "too_long"),  # /MaxLen 40
        ([native(v, ["a"])], "invalid_value", "type"),
    ]
    for fields, code, reason in cases:
        err = fill_error("basic-form.pdf", fields)
        assert err.code == code, (fields, err.code)
        assert (err.details or {}).get("reason") == reason


def test_position_given_for_a_native_field_may_match_within_tolerance():
    f = fields_of("basic-form.pdf")
    v = f["vorname"]
    fill(
        "basic-form.pdf",
        [{**native(v, "x"), "page": 1, "position": {**v["position"], "x": v["position"]["x"] + 0.3}}],
    )


def test_overlay_checks():
    f = fields_of("basic-form.pdf")
    cases = [
        ([overlay("o", 2, 10, 10, 50, 20, "x")], "invalid_geometry", "page"),
        ([overlay("o", 1, 560, 10, 50, 20, "x")], "invalid_geometry", "outside_page"),
        ([{"id": "o", "mode": "overlay", "type": "text", "value": "x"}], "invalid_manifest", None),
        ([overlay("o", 1, 10, 10, 50, 20, "x", type_="radio")], "invalid_manifest", None),
        (
            [native(f["vorname"], "a"), overlay("o", 1, 121, 72, 198, 20, "b")],
            "field_conflict",
            "overlaps_native",
        ),
        ([overlay("o", 1, 10, 10, 50, 20, "", required=True)], "invalid_value", "missing_value"),
        (
            [overlay("o", 1, 10, 10, 50, 20, "x", style={"font_size": 8, "min_font_size": 9})],
            "invalid_manifest",
            None,
        ),
    ]
    for fields, code, reason in cases:
        err = fill_error("basic-form.pdf", fields)
        assert err.code == code, (fields, err.code)
        assert (err.details or {}).get("reason") == reason


def test_field_limit():
    with pytest.raises(DocPipeError) as exc:
        m = parse_manifest(
            json.dumps(
                manifest("static-form.pdf", [overlay(f"o{i}", 1, 10, 10, 20, 20, "x") for i in range(4)])
            )
        )
        fill_pdf(fixture("static-form.pdf"), m, {}, None, max_pages=200, max_fields=3)
    assert exc.value.code == "limit_exceeded"


# --- protection ---------------------------------------------------------------------------------------


def test_open_password_and_preserved_encryption():
    f = fields_of("encrypted-user-password.pdf", USER_PASSWORD)
    fields = [native(f["feld"], "Geheim gefüllt")]
    assert fill_error("encrypted-user-password.pdf", fields).code == "password_required"
    assert fill_error("encrypted-user-password.pdf", fields, password="falsch").code == "invalid_password"
    out = fill("encrypted-user-password.pdf", fields, password=USER_PASSWORD)
    with pytest.raises(pikepdf.PasswordError):
        pikepdf.open(io.BytesIO(out))
    reopened = pikepdf.open(io.BytesIO(out), password=USER_PASSWORD)
    assert reopened.is_encrypted and str(reopened.encryption.stream_method).endswith("aesv3")
    pikepdf.open(io.BytesIO(out), password=OWNER_PASSWORD)  # owner password unchanged too
    assert "Geheim gefüllt" in extract_text(io.BytesIO(out), password=USER_PASSWORD)


def test_restricted_document_is_not_filled_without_the_owner_password():
    f = fields_of("encrypted-restricted.pdf")
    err = fill_error("encrypted-restricted.pdf", [native(f["feld"], "x")])
    assert err.code == "permission_denied" and err.details == {"permission": "fill_forms"}
    err = fill_error("encrypted-restricted.pdf", [overlay("o", 1, 10, 10, 50, 20, "x")])
    assert err.code == "permission_denied" and err.details == {"permission": "modify"}
    out = fill("encrypted-restricted.pdf", [native(f["feld"], "x")], password=OWNER_PASSWORD)
    reopened = pikepdf.open(io.BytesIO(out))  # still opens without a password ...
    assert reopened.is_encrypted and not reopened.allow.modify_form  # ... and keeps its restrictions


def test_signed_and_certified_documents_are_not_changed():
    f = fields_of("signed-certified.pdf")
    err = fill_error("signed-certified.pdf", [native(f["ort"], "Berlin")])
    assert err.code == "signature_conflict" and err.details == {"reason": "signed"}


def test_xfa():
    assert fill_error("xfa-dynamic.pdf", []).code == "xfa_unsupported"
    f = fields_of("xfa-hybrid.pdf")
    out = fill("xfa-hybrid.pdf", [native(f["feld"], "AcroForm")])
    pdf = pikepdf.open(io.BytesIO(out))
    assert "/AcroForm" not in pdf.Root and "AcroForm" in extract_text(io.BytesIO(out))


# --- original, determinism, preview ---------------------------------------------------------------------


def test_template_is_never_modified_and_output_is_deterministic():
    before = fixture("basic-form.pdf")
    f = fields_of("basic-form.pdf")
    first = fill("basic-form.pdf", [native(f["vorname"], "Eins")])
    second = fill("basic-form.pdf", [native(f["vorname"], "Eins")])
    assert fixture("basic-form.pdf") == before and first == second and first != before


def test_preview_uses_the_same_rendering_plus_a_watermark():
    f = fields_of("basic-form.pdf")
    fields = [native(f["vorname"], "Vorschau"), overlay("o", 1, 300, 400, 150, 20, "Frei")]
    final, preview = fill("basic-form.pdf", fields), fill("basic-form.pdf", fields, preview=True)

    def overlay_stream(pdf_bytes):
        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            xobjects = pdf.pages[0].obj.Resources.XObject
            [stream] = [
                x.read_bytes() for name, x in xobjects.items() if str(name).startswith("/DocPipeFill")
            ]
        return stream

    final_ops, preview_ops = overlay_stream(final), overlay_stream(preview)
    assert final_ops.rstrip() in preview_ops and len(preview_ops) > len(final_ops)
    a, b = render(final), render(preview)
    for box in (f["vorname"]["position"], {"x": 300, "y": 400, "width": 150, "height": 20}):
        assert ink(a, box) == ink(b, box, threshold=160)  # field content identical
    assert "PREVIEW" in extract_text(io.BytesIO(preview)) and "PREVIEW" not in extract_text(io.BytesIO(final))


# --- route ------------------------------------------------------------------------------------------------


def _post(client, name: str, m: dict, key: str = FORMS_KEY, images=None, **data):
    files = [("file", ("t.pdf", fixture(name), "application/pdf"))]
    for ref, content in (images or {}).items():
        files.append(("images", (ref, content, "image/png")))
    return client.post(
        FILL_URL, headers=auth_headers(key), files=files, data={"manifest": json.dumps(m), **data}
    )


def test_route_returns_the_pdf(forms_client, caplog):  # noqa: F811
    caplog.set_level(logging.INFO)
    f = fields_of("basic-form.pdf")
    m = manifest(
        "basic-form.pdf",
        [
            native(f["vorname"], "Geheimer Wert"),
            overlay("sig", 1, 300, 500, 100, 50, type_="signature_image", image_ref="sig.png"),
        ],
    )
    response = _post(forms_client, "basic-form.pdf", m, images={"sig.png": png(100, 50)})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/pdf"
    assert (
        response.headers["cache-control"] == "no-store" and response.headers["x-docpipe-preview"] == "false"
    )
    assert "Geheimer Wert" in extract_text(io.BytesIO(response.content))
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("forms_fill"))
    assert "native=1 overlay=1 images=1" in line and "status=success" in line
    assert "Geheimer" not in caplog.text and "sig.png" not in caplog.text


def test_route_requires_forms_flag(forms_client):  # noqa: F811
    response = _post(forms_client, "basic-form.pdf", manifest("basic-form.pdf", []), key=VALID_KEY)
    assert response.status_code == 403 and response.json()["code"] == "forms_disabled"


def test_route_errors_carry_details_but_no_values(forms_client):  # noqa: F811
    m = manifest("basic-form.pdf", [overlay("lang", 1, 300, 400, 200, 20, "Viel zu langer Wert " * 5)])
    body = _post(forms_client, "basic-form.pdf", m).json()
    assert body == {
        "ok": False,
        "code": "text_overflow",
        "message": "The value does not fit into the field.",
        "details": {"field_id": "lang"},
    }
    assert "Viel zu" not in json.dumps(body)
    body = _post(forms_client, "basic-form.pdf", {**m, "sha256": "f" * 64}).json()
    assert body["code"] == "hash_mismatch" and "details" not in body


def test_route_rejects_bad_parts(forms_client):  # noqa: F811
    m = manifest("static-form.pdf", [])
    assert (
        _post(forms_client, "static-form.pdf", m, images={"../x.png": png(4, 4)}).json()["code"]
        == "invalid_image"
    )
    response = forms_client.post(
        FILL_URL,
        headers=auth_headers(FORMS_KEY),
        files={"file": ("t.pdf", fixture("static-form.pdf"), "application/pdf")},
    )
    assert response.status_code == 400 and response.json()["code"] == "invalid_file"  # manifest missing
    response = forms_client.post(
        FILL_URL,
        headers=auth_headers(FORMS_KEY),
        files={"file": ("t.pdf", b"not a pdf", "application/pdf")},
        data={"manifest": json.dumps(m)},
    )
    assert response.json()["code"] == "invalid_file"


def test_route_preview_header(forms_client):  # noqa: F811
    response = _post(forms_client, "static-form.pdf", manifest("static-form.pdf", [], preview=True))
    assert response.status_code == 200 and response.headers["x-docpipe-preview"] == "true"


def test_route_password(forms_client):  # noqa: F811
    f = fields_of("encrypted-user-password.pdf", USER_PASSWORD)
    m = manifest("encrypted-user-password.pdf", [native(f["feld"], "x")])
    assert _post(forms_client, "encrypted-user-password.pdf", m).json()["code"] == "password_required"
    response = _post(forms_client, "encrypted-user-password.pdf", m, password=USER_PASSWORD)
    assert response.status_code == 200 and USER_PASSWORD.encode() not in response.content


def test_route_removes_temp_files(forms_client, tmp_path, monkeypatch):  # noqa: F811
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    f = fields_of("basic-form.pdf")
    _post(
        forms_client,
        "basic-form.pdf",
        manifest("basic-form.pdf", [native(f["vorname"], "x")]),
        images={"a.png": png(4, 4)},
    )
    _post(forms_client, "basic-form.pdf", manifest("basic-form.pdf", [native(f["vorname"], "x" * 500)]))
    assert list(tmp_path.iterdir()) == []


# --- OpenAPI --------------------------------------------------------------------------------------------


def test_manifest_schema_is_published_and_matches_validation(forms_client):  # noqa: F811
    spec = forms_client.get("/openapi.json").json()
    op = spec["paths"][FILL_URL]["post"]
    schema = op["x-docpipe-manifest-schema"]
    f = fields_of("basic-form.pdf")
    good = manifest(
        "basic-form.pdf",
        [
            native(f["vorname"], "x", style={"color": "#102030"}),
            overlay("o", 1, 1, 1, 10, 10, type_="stamp_image", image_ref="a.png"),
        ],
    )
    jsonschema.validate(good, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**good, "unknown": 1}, schema)
    assert "application/pdf" in op["responses"]["200"]["content"]
    assert sorted(op["responses"]) == ["200", "400", "401", "403", "409", "413", "422"]


def test_fill_error_matches_the_published_error_schema(forms_client):  # noqa: F811
    spec = forms_client.get("/openapi.json").json()
    error = spec["paths"][FILL_URL]["post"]["responses"]["422"]["content"]["application/json"]["schema"]
    f = fields_of("basic-form.pdf")
    body = _post(
        forms_client, "basic-form.pdf", manifest("basic-form.pdf", [native(f["vorname"], "")])
    ).json()
    jsonschema.validate(body, {**error, "components": spec["components"]})
