"""Generator for the synthetic reference PDFs in tests/fixtures/forms/ (Phase 2.1a).

All content is synthetic. The committed PDFs - not this generator - are what
the tests read, so a pikepdf/qpdf upgrade cannot silently move the
references. Regenerate deliberately with:

    python -m tests.forms_fixtures

and review the diff of tests/test_forms_inspect.py expectations afterwards.

Every widget rectangle below is given in PDF user space (bottom-left origin);
the expected visible positions in the tests are computed by hand from these
numbers, not by DocPipe's own geometry code.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pikepdf
from pikepdf import Array, Dictionary, Name, String

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "forms"
A4 = (595.0, 842.0)

USER_PASSWORD = "synthetic-user-pw"
OWNER_PASSWORD = "synthetic-owner-pw"


def _new() -> pikepdf.Pdf:
    pdf = pikepdf.new()
    pdf.Root.AcroForm = pdf.make_indirect(
        Dictionary(Fields=Array(), DA=String("/Helv 0 Tf 0 g"),
                   DR=Dictionary(Font=Dictionary(Helv=_helvetica(pdf))))
    )
    return pdf


def _helvetica(pdf: pikepdf.Pdf):
    return pdf.make_indirect(Dictionary(Type=Name.Font, Subtype=Name.Type1, BaseFont=Name.Helvetica,
                                        Encoding=Name.WinAnsiEncoding))


def _page(pdf, size=A4, texts=(), rotate=None, cropbox=None, mediabox=None):
    page = pdf.add_blank_page(page_size=size)
    if mediabox is not None:
        page.obj.MediaBox = Array(list(mediabox))
    ops = b"".join(f"BT /F1 11 Tf {x} {y} Td ({t}) Tj ET\n".encode("latin-1") for x, y, t in texts)
    page.obj.Contents = pdf.make_stream(ops)
    page.obj.Resources = Dictionary(Font=Dictionary(F1=_helvetica(pdf)))
    if rotate is not None:
        page.obj.Rotate = rotate
    if cropbox is not None:
        page.obj.CropBox = Array(list(cropbox))
    return page


def _attach(page, widget) -> None:
    if "/Annots" not in page.obj:
        page.obj.Annots = Array()
    page.obj.Annots.append(widget)


def _appearance(pdf, states: list[str], w: float, h: float):
    def stream(fill: bool):
        s = pikepdf.Stream(pdf, b"0 g 1 1 %f %f re f" % (w - 2, h - 2) if fill else b"")
        s.Type, s.Subtype, s.BBox = Name.XObject, Name.Form, Array([0, 0, w, h])
        return s
    normal = Dictionary({f"/{state}": stream(True) for state in states})
    normal.Off = stream(False)
    return Dictionary(N=normal)


def text_field(pdf, page, name, rect, *, tu=None, ff=0, maxlen=None, flags=4):
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Tx, T=String(name),
                                          Rect=Array(list(rect)), F=flags, P=page.obj,
                                          DA=String("/Helv 10 Tf 0 g")))
    if tu is not None:
        widget.TU = String(tu)
    if ff:
        widget.Ff = ff
    if maxlen is not None:
        widget.MaxLen = maxlen
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)
    return widget


def checkbox(pdf, page, name, rect, on="Yes", ff=0):
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn, T=String(name),
                                          Rect=Array(list(rect)), F=4, P=page.obj, AS=Name.Off,
                                          AP=_appearance(pdf, [on], w, h)))
    if ff:
        widget.Ff = ff
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)
    return widget


def radio_group(pdf, page, name, options: list[tuple[str, tuple]]):
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
    return parent


def combo(pdf, page, name, rect, options):
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Ch, T=String(name),
                                          Ff=1 << 17, Rect=Array(list(rect)), F=4, P=page.obj,
                                          Opt=Array([Array([String(v), String(lbl)]) for v, lbl in options])))
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)
    return widget


def push_button(pdf, page, name, rect):
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn, T=String(name),
                                          Ff=1 << 16, Rect=Array(list(rect)), F=4, P=page.obj))
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)


def signature_field(pdf, page, name, rect, signed: bool):
    widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Sig, T=String(name),
                                          Rect=Array(list(rect)), F=4, P=page.obj))
    if signed:
        widget.V = pdf.make_indirect(Dictionary(Type=Name.Sig, Filter=Name("/Adobe.PPKLite"),
                                                SubFilter=Name("/adbe.pkcs7.detached"),
                                                ByteRange=Array([0, 0, 0, 0]), Contents=String(b"\x00" * 32)))
    pdf.Root.AcroForm.Fields.append(widget)
    _attach(page, widget)
    return widget


def _bytes(pdf, **save_kwargs) -> bytes:
    out = io.BytesIO()
    pdf.save(out, static_id=True, **save_kwargs)
    return out.getvalue()


# --- reference documents -----------------------------------------------------------------------

def basic_form() -> bytes:
    """One A4 page: label + text field, checkbox, radio group, combo box, push button."""
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Vorname:"), (50, 720, "Einverstanden"), (50, 680, "Anrede"),
                             (50, 640, "Land")])
    text_field(pdf, page, "vorname", (120, 750, 320, 770), tu="Vorname", ff=1 << 1, maxlen=40)
    text_field(pdf, page, "bemerkung", (120, 520, 520, 600), ff=1 << 12)
    checkbox(pdf, page, "einverstanden", (150, 715, 164, 729), on="Ja")
    radio_group(pdf, page, "anrede", [("frau", (120, 675, 134, 689)), ("herr", (160, 675, 174, 689))])
    combo(pdf, page, "land", (120, 635, 220, 655), [("DE", "Deutschland"), ("AT", "Österreich")])
    push_button(pdf, page, "drucken", (400, 50, 480, 70))
    return _bytes(pdf)


def rotated_pages() -> bytes:
    """Four A4 pages with /Rotate 0, 90, 180, 270; same label and field rect on each."""
    pdf = _new()
    for rotation in (0, 90, 180, 270):
        page = _page(pdf, texts=[(50, 760, "Vorname:")], rotate=rotation)
        text_field(pdf, page, f"vorname_r{rotation}", (120, 750, 320, 770))
    return _bytes(pdf)


def cropbox_offset() -> bytes:
    """MediaBox with a non-zero origin plus a CropBox that hides a margin."""
    pdf = _new()
    page = _page(pdf, mediabox=(10, 20, 605, 862), cropbox=(40, 60, 575, 822),
                 texts=[(60, 790, "Name")])
    text_field(pdf, page, "name", (100, 780, 300, 800))
    text_field(pdf, page, "ragt_heraus", (500, 100, 620, 120))  # partly beyond the CropBox
    text_field(pdf, page, "ausserhalb", (700, 100, 800, 120))  # entirely beyond it
    return _bytes(pdf)


def multi_widget_and_hierarchy() -> bytes:
    """Field 'person.name' with two widgets on pages 1 and 2; inherited /Rotate 90 from /Pages."""
    pdf = _new()
    p1 = _page(pdf, texts=[(50, 760, "Name")])
    p2 = _page(pdf, texts=[(50, 400, "Name (Wiederholung)")])
    person = pdf.make_indirect(Dictionary(T=String("person"), Kids=Array()))
    name = pdf.make_indirect(Dictionary(T=String("name"), FT=Name.Tx, Parent=person, Kids=Array()))
    person.Kids.append(name)
    for page, rect in ((p1, (120, 750, 320, 770)), (p2, (300, 390, 500, 410))):
        widget = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, Parent=name,
                                              Rect=Array(list(rect)), F=4, P=page.obj))
        name.Kids.append(widget)
        _attach(page, widget)
    pdf.Root.AcroForm.Fields.append(person)
    hidden_page = _page(pdf, texts=[(50, 760, "Intern")])
    text_field(pdf, hidden_page, "intern", (120, 750, 320, 770), flags=4 | 2)  # hidden widget
    pdf.Root.Pages.Rotate = 90  # inherited by all pages without their own /Rotate
    return _bytes(pdf)


def static_scan_like() -> bytes:
    """Two pages without any form; page 2 has no text layer at all."""
    pdf = pikepdf.new()
    _page(pdf, texts=[(50, 760, "Antrag ohne Formularfelder")])
    _page(pdf)
    return _bytes(pdf)


def encrypted_user_password() -> bytes:
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Geheim")])
    text_field(pdf, page, "feld", (120, 750, 320, 770))
    return _bytes(pdf, encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD, R=6))


def encrypted_restricted() -> bytes:
    """No open password, but the owner forbids form filling and text extraction."""
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Nur lesen")])
    text_field(pdf, page, "feld", (120, 750, 320, 770))
    allow = pikepdf.Permissions(extract=False, modify_form=False, modify_annotation=False, modify_other=False)
    return _bytes(pdf, encryption=pikepdf.Encryption(user="", owner=OWNER_PASSWORD, R=6, allow=allow))


def signed_and_certified() -> bytes:
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Unterschrift")])
    text_field(pdf, page, "ort", (120, 750, 320, 770))
    sig = signature_field(pdf, page, "unterschrift", (120, 100, 320, 160), signed=True)
    signature_field(pdf, page, "zweitunterschrift", (350, 100, 550, 160), signed=False)
    pdf.Root.Perms = Dictionary(DocMDP=sig.V)
    return _bytes(pdf)


def xfa_hybrid() -> bytes:
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "XFA hybrid")])
    text_field(pdf, page, "feld", (120, 750, 320, 770))
    pdf.Root.AcroForm.XFA = pdf.make_stream(b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
    return _bytes(pdf)


def xfa_dynamic() -> bytes:
    pdf = _new()
    _page(pdf, texts=[(50, 760, "Please wait...")])
    pdf.Root.AcroForm.XFA = pdf.make_stream(b"<xdp:xdp xmlns:xdp='http://ns.adobe.com/xdp/'/>")
    pdf.Root.NeedsRendering = True
    return _bytes(pdf)


def invalid_rotation() -> bytes:
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Schief")], rotate=45)
    text_field(pdf, page, "feld", (120, 750, 320, 770))
    return _bytes(pdf)


def many_pages(count: int = 5) -> bytes:
    pdf = pikepdf.new()
    for n in range(count):
        _page(pdf, texts=[(50, 760, f"Seite {n + 1}")])
    return _bytes(pdf)


REFERENCE_PDFS = {
    "basic-form.pdf": basic_form,
    "rotated-pages.pdf": rotated_pages,
    "cropbox-offset.pdf": cropbox_offset,
    "multi-widget-hierarchy.pdf": multi_widget_and_hierarchy,
    "static-no-fields.pdf": static_scan_like,
    "encrypted-user-password.pdf": encrypted_user_password,
    "encrypted-restricted.pdf": encrypted_restricted,
    "signed-certified.pdf": signed_and_certified,
    "xfa-hybrid.pdf": xfa_hybrid,
    "xfa-dynamic.pdf": xfa_dynamic,
    "invalid-rotation.pdf": invalid_rotation,
    "five-pages.pdf": many_pages,
}


def main(target: Path = FIXTURE_DIR) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for filename, build in REFERENCE_PDFS.items():
        (target / filename).write_bytes(build())
        print(f"wrote {target / filename}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE_DIR)
