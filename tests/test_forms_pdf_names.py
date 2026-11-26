"""PDF names that are not valid UTF-8 (e.g. byte 0xDF) in inspect and fill.

Regression: a real form whose checkbox appearance state was `/au#DFer`
("außer" in Windows-1252) made forms/inspect answer HTTP 500 - pikepdf returns
such names with lone surrogates, which cannot be encoded as JSON - and could
not have been filled either (`pikepdf.Name()` rejects them). The documents here
are synthetic and built in memory.
"""

from __future__ import annotations

import hashlib
import io
import json

import pikepdf
import pytest
from pikepdf import Array, Dictionary, Name, String

import system.forms.fill as fill_mod
from system.errors import DocPipeError
from system.forms import names
from system.forms.fill import fill_pdf, load_images, parse_manifest
from system.forms.inspect import inspect_pdf
from tests.forms_fixtures import _attach, _bytes, _new, _page
from tests.test_forms_fill import ink, render
from tests.test_forms_inspect import FORMS_KEY, forms_client, post_inspect  # noqa: F401 - fixture

LIMITS = {"max_pages": 200, "max_fields": 2000}

LATIN1_SZ = b"Wei\xdf"  # "Weiß" in Latin-1 / Windows-1252: not valid UTF-8
UTF8_SZ = "Weiß".encode()  # the same word in UTF-8
LITERAL_HEX = b"Wei#DF"  # a name that literally contains "#DF"


# --- names: presentation and its inverse ------------------------------------------------------------

@pytest.mark.parametrize("raw, text", [
    (b"Yes", "Yes"),
    (b"Ja", "Ja"),
    ("Grüße".encode(), "Grüße"),  # valid UTF-8 umlauts stay unchanged
    (b"Sp ace", "Sp ace"),
    (b"A#B", "A#B"),  # a lone '#' stays unchanged
    (b"0", "0"),
    (LATIN1_SZ, "Wei#DF"),
    (b"au\xdfer", "au#DFer"),
    (LITERAL_HEX, "Wei#23DF"),  # literal '#DF' must not collide with byte 0xDF
    (b"\xdf#x\xff", "#DF#23x#FF"),
    ("Grü".encode() + b"\xdf", "Grü#DF"),  # valid characters stay readable in the escaped form
])
def test_name_text_is_json_safe_and_reversible(raw, text):
    key = "/" + raw.decode("utf-8", "surrogateescape")  # how pikepdf returns dictionary keys
    assert names.name_text(key) == text
    assert names.name_text(names.make_name(raw)) == text  # same result for a Name object
    text.encode("utf-8")  # never a lone surrogate
    assert names.name_bytes_from_text(text) == raw
    assert names.name_bytes(names.make_name(raw)) == raw  # the PDF name is rebuilt byte for byte


def test_distinct_names_never_share_a_value():
    raws = [LATIN1_SZ, UTF8_SZ, LITERAL_HEX, b"Wei#23DF", b"Wei", b"Wei\xdf\xdf", b"wei\xdf",
            b"A#B", b"A#23B"]
    texts = [names.name_text("/" + r.decode("utf-8", "surrogateescape")) for r in raws]
    assert len(set(texts)) == len(raws)
    assert [names.name_bytes_from_text(t) for t in texts] == raws


@pytest.mark.parametrize("text", [
    "Wei#df",  # escapes are always upper-case
    "Wei#DF#",  # in the escaped form every '#' starts an escape
    "#DF#G1",
    "Wei#2F",  # '/' (0x2F) is valid UTF-8 and is never escaped
    "Wei\udcdf",  # a lone surrogate
])
def test_values_that_inspect_cannot_produce_are_rejected(text):
    assert names.name_bytes_from_text(text) is None


@pytest.mark.parametrize("text, raw", [("Wei#", b"Wei#"), ("Wei#D", b"Wei#D"), ("Wei#GG", b"Wei#GG"),
                                       ("Wei#23df", b"Wei#df")])
def test_a_hash_without_two_hex_digits_is_a_plain_character(text, raw):
    assert names.name_bytes_from_text(text) == raw
    assert names.name_text("/" + raw.decode()) == text


def test_labels_are_readable_but_never_used_as_identity():
    assert names.name_label("/" + LATIN1_SZ.decode("utf-8", "surrogateescape")) == "Weiß"
    assert names.name_label("/" + UTF8_SZ.decode()) == "Weiß"
    assert names.name_label("/" + b"\x81".decode("utf-8", "surrogateescape")) == "#81"  # undefined in cp1252


def test_json_safe_replaces_and_counts_lone_surrogates():
    replaced = [0]
    out = names.json_safe({"a": ["ok", "x\udcdfy"], "b": {"c": "\ud800"}, "n": 1}, replaced)
    assert out == {"a": ["ok", "x�y"], "b": {"c": "�"}, "n": 1} and replaced == [2]
    json.dumps(out, ensure_ascii=False).encode("utf-8")


# --- synthetic form with such names ----------------------------------------------------------------

def _appearance(pdf, states: list[bytes], w: float, h: float) -> Dictionary:
    def stream(fill: bool):
        s = pikepdf.Stream(pdf, b"0 g 1 1 %f %f re f" % (w - 2, h - 2) if fill else b"")
        s.Type, s.Subtype, s.BBox = Name.XObject, Name.Form, Array([0, 0, w, h])
        return s
    normal = Dictionary()
    for state in states:
        normal[names.make_name(state)] = stream(True)
    normal.Off = stream(False)
    return Dictionary(N=normal)


def _checkbox(pdf, page, name: str, rect, on: bytes):
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn, T=String(name),
                                          Rect=Array(list(rect)), F=4, P=page.obj, AS=Name.Off,
                                          AP=_appearance(pdf, [on], w, h)))
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)


def _radio(pdf, page, name: str, options: list[tuple[bytes, tuple]]):
    radio_flags = (1 << 15) | (1 << 14)  # Radio + NoToggleToOff
    parent = pdf.make_indirect(Dictionary(FT=Name.Btn, T=String(name), Ff=radio_flags, Kids=Array()))
    for state, rect in options:
        w, h = rect[2] - rect[0], rect[3] - rect[1]
        kid = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, Parent=parent,
                                           Rect=Array(list(rect)), F=4, P=page.obj, AS=Name.Off,
                                           AP=_appearance(pdf, [state], w, h)))
        parent.Kids.append(kid)
        _attach(page, kid)
    pdf.Root.AcroForm.Fields.append(parent)


def names_form() -> bytes:
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Ausser"), (50, 720, "Farbe"), (50, 680, "Gruesse"),
                             (50, 640, "Raute")])
    _checkbox(pdf, page, "ausser", (150, 755, 164, 769), b"au\xdfer")
    # Three different names that look alike: Latin-1 byte 0xDF, UTF-8 "ß", literal "#DF".
    _radio(pdf, page, "farbe", [(LATIN1_SZ, (120, 715, 134, 729)), (UTF8_SZ, (160, 715, 174, 729)),
                                (LITERAL_HEX, (200, 715, 214, 729))])
    _checkbox(pdf, page, "grüße", (150, 675, 164, 689), "Grüße".encode())
    _checkbox(pdf, page, "raute", (150, 635, 164, 649), b"A#B")
    return _bytes(pdf)


def _fields(pdf: bytes) -> dict[str, dict]:
    data = inspect_pdf(pdf, None, **LIMITS)
    out = {}
    for f in data["fields"]:
        out[f["input_name"] if f["type"] != "radio" else f"{f['input_name']}/{f['export_value']}"] = f
    return out


def test_inspect_reports_reversible_values_and_valid_json():
    pdf = names_form()
    data = inspect_pdf(pdf, None, **LIMITS)
    json.dumps(data, ensure_ascii=False).encode("utf-8")  # used to raise UnicodeEncodeError
    f = _fields(pdf)
    assert f["ausser"]["export_value"] == "au#DFer"
    assert f["ausser"]["options"] == [{"value": "au#DFer", "label": "außer"}]
    radio = f["farbe/Wei#DF"]
    assert [o["value"] for o in radio["options"]] == ["Wei#DF", "Weiß", "Wei#23DF"]  # three, not merged
    assert [o["label"] for o in radio["options"]] == ["Weiß", "Weiß", "Wei#DF"]
    assert {f["farbe/Weiß"]["export_value"], f["farbe/Wei#23DF"]["export_value"]} == {"Weiß", "Wei#23DF"}
    # Normal names are reported exactly as before.
    assert f["grüße"]["export_value"] == "Grüße"
    assert f["grüße"]["options"] == [{"value": "Grüße", "label": "Grüße"}]
    assert f["raute"]["export_value"] == "A#B"
    assert not [w for w in data["warnings"] if w["code"] == "text_unencodable_replaced"]


def test_inspect_route_answers_200(forms_client):  # noqa: F811
    response = post_inspect(forms_client, names_form())
    assert response.status_code == 200
    values = {o["value"] for f in response.json()["data"]["fields"] for o in f["options"]}
    assert {"au#DFer", "Wei#DF", "Weiß", "Wei#23DF", "Grüße", "A#B"} <= values


def _fill(pdf: bytes, entries: list[dict]) -> bytes:
    m = parse_manifest(json.dumps({"schema_version": "1", "sha256": hashlib.sha256(pdf).hexdigest(),
                                   "fields": entries, "output": {"flatten": True}}))
    return fill_pdf(pdf, m, load_images({}), None, **LIMITS).pdf


def _entry(field: dict, value) -> dict:
    return {"id": field["id"], "mode": "native", "type": field["type"], "native_ref": field["native_ref"],
            "value": value}


def _states_after_fill(pdf: bytes, entries: list[dict], monkeypatch) -> dict[str, bytes]:
    """/AS of every widget by field name, read before flattening removes the widgets."""
    monkeypatch.setattr(fill_mod, "_flatten", lambda _pdf: None)
    out = _fill(pdf, entries)
    monkeypatch.undo()
    result = {}
    with pikepdf.open(io.BytesIO(out)) as filled:
        for annot in filled.pages[0].obj.Annots:
            field = annot.get("/Parent", annot)
            key = f"{field.T}" + (f"#{len(result)}" if "/Parent" in annot else "")
            result[key] = names.name_bytes(annot.AS)
    return result


@pytest.mark.parametrize("value, raw", [("Wei#DF", LATIN1_SZ), ("Weiß", UTF8_SZ), ("Wei#23DF", LITERAL_HEX)])
def test_fill_sets_exactly_the_original_radio_state(value, raw, monkeypatch):
    pdf = names_form()
    f = _fields(pdf)
    states = _states_after_fill(pdf, [_entry(f["farbe/Wei#DF"], value)], monkeypatch)
    radio = [v for k, v in states.items() if k.startswith("farbe")]
    assert sorted(radio) == sorted([raw, b"Off", b"Off"])


def test_fill_checkboxes_with_escaped_and_unicode_values(monkeypatch):
    pdf = names_form()
    f = _fields(pdf)
    entries = [_entry(f["ausser"], True), _entry(f["grüße"], "Grüße"), _entry(f["raute"], "A#B")]
    states = _states_after_fill(pdf, entries, monkeypatch)
    assert states["ausser"] == b"au\xdfer"
    assert states["grüße"] == "Grüße".encode() and states["raute"] == b"A#B"
    assert _states_after_fill(pdf, [_entry(f["ausser"], "au#DFer")], monkeypatch)["ausser"] == b"au\xdfer"


def test_filled_and_flattened_output_shows_the_chosen_state():
    pdf = names_form()
    f = _fields(pdf)
    image = render(_fill(pdf, [_entry(f["ausser"], True), _entry(f["farbe/Wei#DF"], "Wei#23DF")]))
    assert ink(image, f["ausser"]["position"]) is not None
    assert ink(image, f["farbe/Wei#23DF"]["position"]) is not None
    assert ink(image, f["farbe/Wei#DF"]["position"]) is None
    assert ink(image, f["farbe/Weiß"]["position"]) is None


@pytest.mark.parametrize("value", ["Wei#df", "Weiss", "Weiß#", "außer"])
def test_fill_rejects_values_that_are_not_an_option(value):
    pdf = names_form()
    f = _fields(pdf)
    target = f["ausser"] if value.startswith("au") else f["farbe/Wei#DF"]
    with pytest.raises(DocPipeError) as exc:
        _fill(pdf, [_entry(target, value)])
    assert exc.value.code == "invalid_value" and exc.value.details["reason"] == "option"
