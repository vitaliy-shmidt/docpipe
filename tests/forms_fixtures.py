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


# --- Phase 2.1b: static forms and scans ---------------------------------------------------------

# Static form on a landscape display canvas of 842 x 595 pt (bottom-left origin, as drawn).
# GROUND TRUTH (visible coordinates, top-left origin) is listed in STATIC_FORM_EXPECTED; the
# decoys in STATIC_FORM_DECOYS must NOT be proposed.
STATIC_CANVAS = (842.0, 595.0)


def _static_form_ops() -> bytes:
    def text(x, y, s, size=11):
        return f"BT /F1 {size} Tf {x} {y} Td ({s}) Tj ET\n"

    def line(x0, y0, x1, y1):
        return f"0.8 w {x0} {y0} m {x1} {y1} l S\n"

    def rect(x, y, w, h):
        return f"0.8 w {x} {y} {w} {h} re S\n"

    ops = [
        rect(15, 15, 812, 565),  # decoy: page frame
        text(50, 555, "Antrag auf Mitgliedschaft", 14), line(50, 551, 230, 551),  # decoy: underlined heading
        line(20, 535, 822, 535),  # decoy: page-wide separator
        text(50, 505, "Name:"), line(100, 502, 330, 502),  # 1 underline, label left
        text(400, 505, "Geburtsdatum: ____________"),  # 2 placeholder
        text(50, 470, "Strasse und Hausnummer"), rect(50, 440, 280, 20),  # 3 box, label above
        text(400, 447, "PLZ / Ort:"), rect(470, 440, 200, 20),  # 4 box, label left
        text(50, 410, "Bemerkungen"), rect(50, 330, 520, 70),  # 5 multi-line box
        rect(50, 300, 12, 12), text(68, 302, "Ja"),  # 6 checkbox
        rect(110, 300, 12, 12), text(128, 302, "Nein"),  # 7 checkbox
        line(450, 220, 700, 220), text(460, 206, "Ort, Datum, Unterschrift"),  # 8 signature line
        # decoy: a box that already contains text
        rect(50, 150, 300, 40), text(55, 165, "Hinweis: Bitte vollst\xe4ndig ausf\xfcllen."),
    ]
    for x in (450, 600, 750):  # decoy: table grid drawn from single lines
        ops.append(line(x, 100, x, 160))
    for y in (100, 130, 160):
        ops.append(line(450, y, 750, y))
    ops += [text(455, 140, "Position"), text(605, 140, "Betrag")]
    return "".join(ops).encode("latin-1")


# (detection, type, label, x, y, width, height) in visible coordinates of the 842 x 595 display;
# None = derived from glyph boxes (placeholder), checked separately.
STATIC_FORM_EXPECTED = [
    ("underline", "text", "Name", 100.0, 77.0, 230.0, 16.0),
    ("placeholder", "text", "Geburtsdatum", None, None, None, None),
    ("box", "text", "Strasse und Hausnummer", 50.0, 135.0, 280.0, 20.0),
    ("box", "text", "PLZ / Ort", 470.0, 135.0, 200.0, 20.0),
    ("box", "text", "Bemerkungen", 50.0, 195.0, 520.0, 70.0),
    ("checkbox", "checkbox", "Ja", 50.0, 283.0, 12.0, 12.0),
    ("checkbox", "checkbox", "Nein", 110.0, 283.0, 12.0, 12.0),
    ("underline", "text", "Ort, Datum, Unterschrift", 450.0, 359.0, 250.0, 16.0),
]
# decoy areas (x, y, width, height) in visible coordinates - no proposal may overlap them
STATIC_FORM_DECOYS = {
    "heading_underline": (50.0, 30.0, 180.0, 14.0),
    "separator": (20.0, 58.0, 802.0, 2.0),
    "text_box": (50.0, 405.0, 300.0, 40.0),
    "table": (450.0, 435.0, 300.0, 60.0),
}


def _static_page(pdf, mediabox, rotate=None, cropbox=None, cm=None):
    page = pdf.add_blank_page(page_size=(mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]))
    page.obj.MediaBox = Array(list(mediabox))
    ops = _static_form_ops()
    if cm is not None:
        ops = b"q " + cm.encode("ascii") + b" cm\n" + ops + b"Q\n"
    page.obj.Contents = pdf.make_stream(ops)
    page.obj.Resources = Dictionary(Font=Dictionary(F1=_helvetica(pdf)))
    if rotate is not None:
        page.obj.Rotate = rotate
    if cropbox is not None:
        page.obj.CropBox = Array(list(cropbox))
    return page


def static_form() -> bytes:
    """The static form on an unrotated landscape page (842 x 595)."""
    pdf = pikepdf.new()
    _static_page(pdf, (0, 0, 842, 595))
    return _bytes(pdf)


def static_form_rotated() -> bytes:
    """The same form on a PORTRAIT page with /Rotate 90, drawn so that it displays upright.

    For /Rotate 90 the viewer turns the page clockwise: display (X, Y) = user (y, 595 - x), so the
    content is drawn with the inverse matrix  x = 595 - Y, y = X  ->  cm [0 1 -1 0 595 0].
    """
    pdf = pikepdf.new()
    _static_page(pdf, (0, 0, 595, 842), rotate=90, cm="0 1 -1 0 595 0")
    return _bytes(pdf)


def static_form_cropbox() -> bytes:
    """The same form shifted by (30, 40) inside a larger MediaBox whose CropBox shows exactly it."""
    pdf = pikepdf.new()
    _static_page(pdf, (10, 20, 900, 660), cropbox=(30, 40, 872, 635), cm="1 0 0 1 30 40")
    return _bytes(pdf)


def mixed_native_static() -> bytes:
    """A native text field drawn ON an underline, plus a second, purely static underline."""
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Vorname:"), (50, 700, "Nachname:")])
    ops = page.obj.Contents.read_bytes() + b"0.8 w 120 752 m 320 752 l S\n0.8 w 120 692 m 320 692 l S\n"
    page.obj.Contents = pdf.make_stream(ops)
    text_field(pdf, page, "vorname", (120, 752, 320, 770))
    return _bytes(pdf)


def _scan_image(pdf):
    """A grey 'scan' bitmap (no text anywhere)."""
    w, h = 60, 85
    raw = bytes(((x * 7 + y * 3) % 64) + 160 for y in range(h) for x in range(w))
    image = pikepdf.Stream(pdf, raw)
    image.Type, image.Subtype = Name.XObject, Name.Image
    image.Width, image.Height, image.ColorSpace, image.BitsPerComponent = w, h, Name.DeviceGray, 8
    return image


def _scan_page(pdf, mediabox=(0, 0, 595, 842), rotate=None, cropbox=None):
    width, height = mediabox[2] - mediabox[0], mediabox[3] - mediabox[1]
    page = pdf.add_blank_page(page_size=(width, height))
    page.obj.MediaBox = Array(list(mediabox))
    page.obj.Resources = Dictionary(XObject=Dictionary(Im1=_scan_image(pdf)))
    page.obj.Contents = pdf.make_stream(
        f"q {width} 0 0 {height} {mediabox[0]} {mediabox[1]} cm /Im1 Do Q\n".encode("ascii"))
    if rotate is not None:
        page.obj.Rotate = rotate
    if cropbox is not None:
        page.obj.CropBox = Array(list(cropbox))
    return page


def scan_pages() -> bytes:
    """p1 text, p2 A4 scan, p3 A4 scan with /Rotate 90, p4 scan with offset MediaBox and CropBox."""
    pdf = pikepdf.new()
    _page(pdf, texts=[(50, 760, "Deckblatt mit Text")])
    _scan_page(pdf)
    _scan_page(pdf, rotate=90)
    _scan_page(pdf, mediabox=(10, 20, 605, 862), cropbox=(40, 60, 575, 822))
    return _bytes(pdf)


def scan_encrypted() -> bytes:
    pdf = pikepdf.new()
    _scan_page(pdf)
    return _bytes(pdf, encryption=pikepdf.Encryption(user=USER_PASSWORD, owner=OWNER_PASSWORD, R=6))


# --- Phase 2.2: fill ---------------------------------------------------------------------------------

# Every page shows its field UPRIGHT at the same visible position (x 100, y 80, 220 x 24, top-left
# origin) on an A4 page displayed with /Rotate 0/90/180/270. User-space rects are the inverse of
# PageBox.to_visible, computed by hand:
#   0:   x0 = 100,           y0 = 842 - 80 - 24 = 738   -> (100, 738, 320, 762)
#   90:  x0 = Y = 80,        y0 = X = 100               -> (80, 100, 104, 320)
#   180: x1 = 595 - 100,     y0 = Y = 80                -> (275, 80, 495, 104)
#   270: y1 = 842 - 100,     x1 = 595 - 80              -> (491, 522, 515, 742)
ROTATED_FIELD_RECTS = {0: (100, 738, 320, 762), 90: (80, 100, 104, 320),
                       180: (275, 80, 495, 104), 270: (491, 522, 515, 742)}
ROTATED_FIELD_VISIBLE = {"x": 100.0, "y": 80.0, "width": 220.0, "height": 24.0}


def rotated_fields() -> bytes:
    pdf = _new()
    for rotation, rect in ROTATED_FIELD_RECTS.items():
        page = _page(pdf, rotate=rotation)
        text_field(pdf, page, f"feld_r{rotation}", rect)
    return _bytes(pdf)


def appearance_form() -> bytes:
    """A text field whose appearance has a border AND old variable text; a checkbox without any appearance."""
    pdf = _new()
    page = _page(pdf, texts=[(50, 760, "Name:"), (50, 700, "Bestaetigt")])
    field = text_field(pdf, page, "name", (120, 752, 320, 772))
    field.V = String("ALT")
    ap = pikepdf.Stream(pdf, b"0.6 G 1 w 0.5 0.5 199 19 re S\n"
                             b"/Tx BMC q BT /Helv 10 Tf 0 g 2 6 Td (ALT) Tj ET Q EMC\n")
    ap.Type, ap.Subtype, ap.BBox = Name.XObject, Name.Form, Array([0, 0, 200, 20])
    ap.Resources = Dictionary(Font=Dictionary(Helv=pdf.Root.AcroForm.DR.Font.Helv))
    field.AP = Dictionary(N=ap)
    box = pdf.make_indirect(Dictionary(Type=Name.Annot, Subtype=Name.Widget, FT=Name.Btn, T=String("ok"),
                                       Rect=Array([150, 692, 164, 706]), F=4, P=page.obj))
    pdf.Root.AcroForm.Fields.append(box)
    _attach(page, box)
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
    "static-form.pdf": static_form,
    "static-form-rotated.pdf": static_form_rotated,
    "static-form-cropbox.pdf": static_form_cropbox,
    "mixed-native-static.pdf": mixed_native_static,
    "scan-pages.pdf": scan_pages,
    "scan-encrypted.pdf": scan_encrypted,
    "rotated-fields.pdf": rotated_fields,
    "appearance-form.pdf": appearance_form,
}


def main(target: Path = FIXTURE_DIR) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for filename, build in REFERENCE_PDFS.items():
        (target / filename).write_bytes(build())
        print(f"wrote {target / filename}")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else FIXTURE_DIR)
