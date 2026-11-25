"""Phase 2.1b: OCR for scanned pages and proposals for static fields in forms/inspect.

Stirling is replaced by fake OCR runners that open the subset PDF DocPipe really
sends and answer the way Stirling 3.2.2 does (system/forms/ocr.py):

- `ocrmypdf_like`: the original pages plus an invisible text layer drawn upright
  as displayed (OCRmyPDF keeps MediaBox/CropBox/Rotate);
- `tesseract_like`: new unrotated pages of the visible page size (300 dpi pixel
  rounding simulated by a factor), text at the displayed positions;
- `letter_pages`: a geometry that cannot be mapped (Letter instead of A4).

Expected positions are written in DISPLAY coordinates (bottom-left origin of the
visible, rotated page, as a reader sees it); the assertions convert them to the
contract's top-left system by hand: y_top = display_height - Y.
"""

from __future__ import annotations

import io
from dataclasses import replace

import jsonschema
import pikepdf
import pytest

from system.errors import DocPipeError
from system.forms import detect as detect_mod
from system.forms import ocr as ocr_mod
from system.forms.geometry import PageBox
from system.forms.inspect import inspect_pdf
from tests.conftest import auth_headers
from tests.forms_fixtures import (
    FIXTURE_DIR,
    STATIC_FORM_DECOYS,
    STATIC_FORM_EXPECTED,
    USER_PASSWORD,
    _bytes,
    _static_page,
)
from tests.test_forms_inspect import FORMS_KEY, INSPECT_URL, forms_client  # noqa: F401 - fixture

LIMITS = {"max_pages": 200, "max_fields": 2000}
SCAN = "scan-pages.pdf"  # p1 text, p2 A4 scan, p3 A4 scan /Rotate 90, p4 scan with CropBox (535 x 762)
DISPLAY = {2: (595.0, 842.0), 3: (842.0, 595.0), 4: (535.0, 762.0)}  # displayed size per page
WORDS = {2: [(60, 780, "Antrag")], 3: [(70, 540, "Querformat")], 4: [(40, 700, "Beschnitten")]}


def fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def codes(data: dict) -> list[str]:
    return [w["code"] for w in data["warnings"]]


def detected(data: dict) -> list[dict]:
    return [f for f in data["fields"] if f["source"] == "detected"]


# --- fake Stirling OCR ------------------------------------------------------------------------


def _upright_cm(box: PageBox) -> str:
    """Matrix drawing display coordinates (bottom-left of the visible page) into user space."""
    left, bottom, right, top = box.left, box.bottom, box.right, box.top
    return {
        0: f"1 0 0 1 {left} {bottom}",
        90: f"0 1 -1 0 {right} {bottom}",
        180: f"-1 0 0 -1 {right} {top}",
        270: f"0 -1 1 0 {left} {top}",
    }[box.rotation]


def _invisible_text(words, size=11.0, scale=1.0) -> bytes:
    return b"".join(
        f"BT 3 Tr /FOCR {size * scale} Tf {x * scale} {y * scale} Td ({t}) Tj ET\n".encode("latin-1")
        for x, y, t in words)


def _font(pdf):
    return pdf.make_indirect(pikepdf.Dictionary(Type=pikepdf.Name.Font, Subtype=pikepdf.Name.Type1,
                                                BaseFont=pikepdf.Name.Helvetica))


class Runner:
    """Records what DocPipe sends; answers like one of Stirling's OCR paths."""

    def __init__(self, mode: str = "ocrmypdf", words_by_subset_page=None, factor: float = 1.0004):
        self.mode, self.words, self.factor = mode, words_by_subset_page, factor
        self.calls: list[bytes] = []

    def words_for(self, position: int, template_page: int | None = None):
        if self.words is None:
            return WORDS.get(template_page, [])
        return self.words.get(position, [])

    def __call__(self, subset: bytes) -> bytes:
        self.calls.append(subset)
        src = pikepdf.open(io.BytesIO(subset))
        template_pages = getattr(self, "template_pages", None) or list(range(2, 2 + len(src.pages)))
        if self.mode == "error":
            raise DocPipeError("timeout", "Document processing timed out.")
        if self.mode == "not_pdf":
            return b"<html>gateway error</html>"
        if self.mode == "unchanged":
            return subset
        if self.mode == "drop_page":
            del src.pages[-1]
            return _save(src)
        if self.mode == "ocrmypdf":
            for position, page in enumerate(src.pages):
                box = PageBox.from_page(page.obj)
                ops = b"q " + _upright_cm(box).encode() + b" cm\n" + _invisible_text(
                    self.words_for(position, template_pages[position])) + b"Q\n"
                page.contents_add(src.make_stream(ops), prepend=False)
                page.obj.Resources.Font = pikepdf.Dictionary(FOCR=_font(src))
            return _save(src)
        out = pikepdf.new()  # tesseract / letter: freshly rendered, unrotated pages
        for position, page in enumerate(src.pages):
            box = PageBox.from_page(page.obj)
            if self.mode == "letter":
                size, scale = (612.0, 792.0), 1.0
            else:
                size, scale = (box.width * self.factor, box.height * self.factor), self.factor
            new = out.add_blank_page(page_size=size)
            new.obj.Resources = pikepdf.Dictionary(Font=pikepdf.Dictionary(FOCR=_font(out)))
            new.obj.Contents = out.make_stream(
                _invisible_text(self.words_for(position, template_pages[position]), scale=scale))
        return _save(out)


def _save(pdf) -> bytes:
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


def run(name=SCAN, runner=None, **kwargs) -> dict:
    return inspect_pdf(fixture(name), kwargs.pop("password", None), **{**LIMITS, **kwargs}, ocr=runner)


def assert_word(page: dict, word: str, x: float, y_display: float, display_height: float):
    [block] = [b for b in page["text_blocks"] if b["text"] == word]
    p = block["position"]
    baseline_top = display_height - y_display
    assert p["x"] == pytest.approx(x, abs=0.6), block
    assert p["y"] < baseline_top < p["y"] + p["height"], block


# --- OCR: which pages, what is sent ---------------------------------------------------------------


def test_only_pages_without_text_are_sent_with_resolved_geometry():
    runner = Runner()
    run(runner=runner)
    [subset] = runner.calls
    pdf = pikepdf.open(io.BytesIO(subset))
    assert not pdf.is_encrypted and len(pdf.pages) == 3  # p2-p4, not the text page
    boxes = [PageBox.from_page(p.obj) for p in pdf.pages]
    assert [b.rotation for b in boxes] == [0, 90, 0]
    assert (boxes[2].left, boxes[2].bottom, boxes[2].right, boxes[2].top) == (40, 60, 575, 822)
    assert boxes[2].mediabox == (10, 20, 605, 862)


def test_no_ocr_call_when_every_page_has_text():
    runner = Runner()
    data = inspect_pdf(fixture("basic-form.pdf"), None, **LIMITS, ocr=runner)
    assert runner.calls == [] and data["ocr"] == {"status": "not_needed", "pages": []}


# --- OCR: geometry -------------------------------------------------------------------------------


@pytest.mark.parametrize("mode, geometry", [("ocrmypdf", "identical"), ("tesseract", "rescaled")])
def test_ocr_text_is_mapped_onto_the_template_page(mode, geometry):
    data = run(runner=Runner(mode))
    assert data["ocr"]["status"] == "applied"
    assert data["ocr"]["pages"] == [{"page": n, "geometry": geometry, "text_found": True} for n in (2, 3, 4)]
    assert [p["text_source"] for p in data["pages"]] == ["embedded", "ocr", "ocr", "ocr"]
    for number, words in WORDS.items():
        page = data["pages"][number - 1]
        assert (page["width"], page["height"]) == DISPLAY[number]
        for x, y, word in words:
            assert word in page["text"]
            assert_word(page, word, x, y, DISPLAY[number][1])
    assert "no_text_layer" not in codes(data) and "ocr_geometry_mismatch" not in codes(data)
    assert data["text"].split("\n\n") == ["Deckblatt mit Text", "Antrag", "Querformat", "Beschnitten"]


def test_ocr_output_with_other_geometry_gives_text_without_positions():
    data = run(runner=Runner("letter"))
    assert [p["geometry"] for p in data["ocr"]["pages"]] == ["mismatch"] * 3
    for number in (2, 3, 4):
        page = data["pages"][number - 1]
        assert page["text_source"] == "ocr" and page["text"] and page["text_blocks"] == []
        assert {"code": "ocr_geometry_mismatch", "page": number}.items() <= next(
            w for w in data["warnings"]
            if w["code"] == "ocr_geometry_mismatch" and w["page"] == number).items()
    assert detected(data) == []


@pytest.mark.parametrize(
    "template, out, expected",
    [
        ((0, 0, 595, 842, 0), (0, 0, 595, 842, 0), "identical"),
        ((0, 0, 595, 842, 90), (0, 0, 842.3, 595.2, 0), "rescaled"),  # rendered, pixel rounding
        ((0, 0, 595, 842, 0), (0, 0, 612, 792, 0), "mismatch"),  # other paper size
        ((0, 0, 595, 842, 0), (0, 0, 595, 842, 90), "mismatch"),  # rotated by OCR (--rotate-pages)
        ((0, 0, 595, 842, 0), (0, 0, 640, 905, 0), "mismatch"),  # same aspect, > 2 % larger
    ],
)
def test_classify_geometry(template, out, expected):
    def box(x0, y0, x1, y1, rotation):
        pdf = pikepdf.new()
        page = pdf.add_blank_page(page_size=(x1 - x0, y1 - y0))
        page.obj.MediaBox = pikepdf.Array([x0, y0, x1, y1])
        page.obj.Rotate = rotation
        return PageBox.from_page(page.obj)

    assert ocr_mod.classify(box(*template), box(*out))[0] == expected


# --- OCR: failures and limits ----------------------------------------------------------------------


@pytest.mark.parametrize("mode, reason", [("error", "timeout"), ("not_pdf", "ocr_invalid_output"),
                                          ("drop_page", "ocr_invalid_output")])
def test_ocr_failures_are_reported_and_do_not_fail_inspection(mode, reason):
    data = run(runner=Runner(mode))
    assert data["ocr"] == {"status": "failed", "pages": [], "reason": reason}
    assert {"code": "ocr_failed", "reason": reason}.items() <= next(
        w for w in data["warnings"] if w["code"] == "ocr_failed").items()
    assert [p["text_source"] for p in data["pages"]] == ["embedded", "none", "none", "none"]
    assert [w["page"] for w in data["warnings"] if w["code"] == "no_text_layer"] == [2, 3, 4]
    assert data["pages"][0]["text"] == "Deckblatt mit Text"


def test_ocr_that_finds_nothing():
    data = run(runner=Runner("unchanged"))
    assert data["ocr"]["status"] == "applied"
    assert [p["text_found"] for p in data["ocr"]["pages"]] == [False] * 3
    assert [w["page"] for w in data["warnings"] if w["code"] == "ocr_no_text"] == [2, 3, 4]
    assert "no_text_layer" not in codes(data)


def test_ocr_page_limit():
    runner = Runner()
    data = run(runner=runner, max_ocr_pages=2)
    assert len(pikepdf.open(io.BytesIO(runner.calls[0])).pages) == 2
    assert data["ocr"]["status"] == "partial"
    assert [p["page"] for p in data["ocr"]["pages"]] == [2, 3]
    assert {"code": "ocr_page_limit", "count": 1}.items() <= next(
        w for w in data["warnings"] if w["code"] == "ocr_page_limit").items()
    assert [w["page"] for w in data["warnings"] if w["code"] == "no_text_layer"] == [4]
    assert data["pages"][3]["text_source"] == "none"


@pytest.mark.parametrize("skipped", ["disabled", "off"])
def test_ocr_unavailable_or_switched_off(skipped):
    data = inspect_pdf(fixture(SCAN), None, **LIMITS, ocr=None, ocr_skipped=skipped)
    assert data["ocr"] == {"status": skipped, "pages": []}
    assert [w["page"] for w in data["warnings"] if w["code"] == "no_text_layer"] == [2, 3, 4]


def test_encrypted_scan_is_sent_decrypted_without_the_password():
    runner = Runner(words_by_subset_page={0: [(60, 780, "Vertraulich")]})
    runner.template_pages = [1]
    data = inspect_pdf(fixture("scan-encrypted.pdf"), USER_PASSWORD, **LIMITS, ocr=runner)
    [subset] = runner.calls
    assert not pikepdf.open(io.BytesIO(subset)).is_encrypted
    assert USER_PASSWORD.encode() not in subset
    assert data["pages"][0]["text_source"] == "ocr" and data["pages"][0]["text"] == "Vertraulich"


def test_no_ocr_without_text_extraction_permission():
    runner = Runner()
    data = inspect_pdf(fixture("encrypted-restricted.pdf"), None, **LIMITS, ocr=runner)
    assert runner.calls == [] and data["ocr"]["status"] == "not_permitted"
    assert detected(data) == [] and data["detection"] == {"enabled": False, "count": 0}


# --- static field proposals --------------------------------------------------------------------------


def _match(fields, detection, label):
    return [f for f in fields if f["detection"] == detection and f["name"] == label]


@pytest.mark.parametrize("name", ["static-form.pdf", "static-form-rotated.pdf", "static-form-cropbox.pdf"])
def test_static_form_ground_truth(name):
    data = inspect_pdf(fixture(name), None, **LIMITS)
    page = data["pages"][0]
    assert (page["width"], page["height"]) == (842.0, 595.0)
    fields = detected(data)
    # recall: every expected field exactly once, at the hand-computed position
    for detection, field_type, label, x, y, w, h in STATIC_FORM_EXPECTED:
        [f] = _match(fields, detection, label)
        assert f["type"] == field_type and f["source"] == "detected" and f["page"] == 1
        if x is not None:
            assert f["position"] == {"x": x, "y": y, "width": w, "height": h}
    # precision: nothing else, and nothing on a decoy
    assert len(fields) == len(STATIC_FORM_EXPECTED)
    for f in fields:
        p = f["position"]
        for decoy, (dx, dy, dw, dh) in STATIC_FORM_DECOYS.items():
            overlap_w = min(p["x"] + p["width"], dx + dw) - max(p["x"], dx)
            overlap_h = min(p["y"] + p["height"], dy + dh) - max(p["y"], dy)
            assert not (overlap_w > 0 and overlap_h > 0), (decoy, f)


def test_placeholder_covers_the_underscores_after_its_label():
    data = inspect_pdf(fixture("static-form.pdf"), None, **LIMITS)
    [f] = _match(detected(data), "placeholder", "Geburtsdatum")
    [label] = [b for b in data["pages"][0]["text_blocks"] if b["text"].startswith("Geburtsdatum")]
    lp, fp = label["position"], f["position"]
    assert lp["x"] < fp["x"] and fp["x"] + fp["width"] == pytest.approx(lp["x"] + lp["width"], abs=0.5)
    assert fp["y"] == lp["y"] and fp["height"] == lp["height"]


def test_rotated_and_cropped_variants_are_identical_to_the_plain_page():
    plain = inspect_pdf(fixture("static-form.pdf"), None, **LIMITS)
    for name in ("static-form-rotated.pdf", "static-form-cropbox.pdf"):
        other = inspect_pdf(fixture(name), None, **LIMITS)
        assert other["fields"] == plain["fields"], name
        assert other["pages"][0]["text_blocks"] == plain["pages"][0]["text_blocks"], name


@pytest.mark.parametrize("rotation, mediabox, cm", [
    (180, (0, 0, 842, 595), "-1 0 0 -1 842 595"),
    (270, (0, 0, 595, 842), "0 -1 1 0 0 842"),
])
def test_upright_content_on_180_and_270_degree_pages(rotation, mediabox, cm):
    """Display pass inverse for the remaining rotations, generated in-test from the same form."""
    pdf = pikepdf.new()
    _static_page(pdf, mediabox, rotate=rotation, cm=cm)
    data = inspect_pdf(_bytes(pdf), None, **LIMITS)
    plain = inspect_pdf(fixture("static-form.pdf"), None, **LIMITS)
    assert data["pages"][0]["rotation"] == rotation
    # 0.01 pt rounding differences come from computing widths as (842 - x0) - (842 - x1).
    assert_same(data["fields"], plain["fields"])
    assert_same(data["pages"][0]["text_blocks"], plain["pages"][0]["text_blocks"])


def assert_same(a, b, tolerance: float = 0.02, path: str = "$"):
    """Structural equality; floats may differ by `tolerance` points."""
    if isinstance(a, dict):
        assert isinstance(b, dict) and a.keys() == b.keys(), path
        for key in a:
            assert_same(a[key], b[key], tolerance, f"{path}.{key}")
    elif isinstance(a, list):
        assert isinstance(b, list) and len(a) == len(b), path
        for i, (x, y) in enumerate(zip(a, b)):
            assert_same(x, y, tolerance, f"{path}[{i}]")
    elif isinstance(a, float) and not isinstance(a, bool):
        assert b == pytest.approx(a, abs=tolerance), path
    else:
        assert a == b, path


def test_confidence_and_labels():
    fields = {(f["detection"], f["name"]): f for f in detected(inspect_pdf(fixture("static-form.pdf"), None,
                                                                            **LIMITS))}
    assert fields[("underline", "Name")]["confidence"] == 0.85  # label + colon
    assert fields[("box", "PLZ / Ort")]["confidence"] == 0.9
    assert fields[("box", "Strasse und Hausnummer")]["confidence"] == 0.8
    assert fields[("underline", "Ort, Datum, Unterschrift")]["confidence"] == 0.75  # caption below
    assert fields[("box", "Bemerkungen")]["multiline"] is True
    assert fields[("box", "PLZ / Ort")]["label"]["text"] == "PLZ / Ort:"
    for f in fields.values():
        assert f["input_name"] is None and f["native_ref"] is None and f["field_ref"] is None
        assert f["id"].startswith("detected_") and 0 < f["confidence"] <= 0.95


def test_proposal_without_label_has_no_name_and_low_confidence():
    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(595, 842))
    page.obj.Contents = pdf.make_stream(b"0.8 w 100 500 m 300 500 l S\n")
    data = inspect_pdf(_bytes(pdf), None, **LIMITS)
    [f] = detected(data)
    assert f["name"] is None and f["label"] is None and f["confidence"] == 0.45
    warning = next(w for w in data["warnings"] if w["code"] == "fields_detected")
    assert warning["count"] == 1 and warning["low_confidence_count"] == 1


def test_native_fields_suppress_overlapping_proposals_and_keep_their_ids():
    data = inspect_pdf(fixture("mixed-native-static.pdf"), None, **LIMITS)
    native = [f for f in data["fields"] if f["source"] == "native"]
    assert [f["id"] for f in native] == ["field_001"]
    [proposal] = detected(data)
    assert proposal["name"] == "Nachname" and proposal["id"] == "detected_001"
    assert proposal["position"] == {"x": 120.0, "y": 134.0, "width": 200.0, "height": 16.0}  # 842 - 692 - 16


def test_detection_off():
    data = inspect_pdf(fixture("static-form.pdf"), None, **LIMITS, detect=False)
    assert detected(data) == [] and data["detection"] == {"enabled": False, "count": 0}
    assert "fields_detected" not in codes(data)


def test_ocr_text_feeds_placeholder_detection_on_scans():
    runner = Runner(words_by_subset_page={0: [(60, 700, "Name: ____________")]})
    data = run(runner=runner)
    [proposal] = [f for f in detected(data) if f["page"] == 2]
    assert proposal["detection"] == "placeholder" and proposal["name"] == "Name"
    assert proposal["position"]["y"] < 842 - 700 < proposal["position"]["y"] + proposal["position"]["height"]


def test_detection_page_limit(monkeypatch):
    monkeypatch.setattr(detect_mod, "MAX_PER_PAGE", 3)
    data = inspect_pdf(fixture("static-form.pdf"), None, **LIMITS)
    assert len(detected(data)) == 3 and "detection_limit" in codes(data)


# --- route ------------------------------------------------------------------------------------------


def _post(client, name=SCAN, **data):
    files = {"file": ("x.pdf", fixture(name), "application/pdf")}
    return client.post(INSPECT_URL, headers=auth_headers(FORMS_KEY), files=files, data=data or None)


@pytest.fixture
def stirling_ocr(forms_client):  # noqa: F811 - pytest fixture injection
    """Routes the fake Stirling client's ocr_pdf through an ocrmypdf-like runner."""
    fake = forms_client.fake_stirling["client"]
    runner = Runner()
    seen = {}

    def ocr_pdf(file_bytes, filename, languages):
        seen.update(filename=filename, languages=list(languages))
        return runner(file_bytes)

    fake.ocr_pdf = ocr_pdf
    return forms_client, runner, seen


def test_route_uses_stirling_ocr_with_configured_languages(stirling_ocr):
    client, runner, seen = stirling_ocr
    body = _post(client).json()
    assert body["ok"] is True and body["data"]["ocr"]["status"] == "applied"
    assert seen == {"filename": "document.pdf", "languages": ["deu", "eng"]} and len(runner.calls) == 1


def test_route_ocr_off_and_detect_off(stirling_ocr):
    client, runner, _ = stirling_ocr
    data = _post(client, ocr="off").json()["data"]
    assert runner.calls == [] and data["ocr"]["status"] == "off"
    data = _post(client, name="static-form.pdf", detect="off").json()["data"]
    assert data["detection"] == {"enabled": False, "count": 0}


def test_route_server_ocr_disabled(stirling_ocr):
    client, runner, _ = stirling_ocr
    settings = client.app.state.settings
    client.app.state.settings = replace(
        settings, stirling=replace(settings.stirling, ocr=replace(settings.stirling.ocr, enabled=False)))
    data = _post(client).json()["data"]
    assert runner.calls == [] and data["ocr"]["status"] == "disabled"


@pytest.mark.parametrize("field, value", [("ocr", "force"), ("detect", "yes")])
def test_route_rejects_unknown_modes(stirling_ocr, field, value):
    client, runner, _ = stirling_ocr
    response = _post(client, **{field: value})
    assert response.status_code == 400 and response.json()["code"] == "invalid_request"
    assert runner.calls == []


def test_route_stirling_outage_is_a_warning_not_an_error(forms_client):  # noqa: F811
    forms_client.fake_stirling["client"].ocr_mode = "unavailable"
    response = _post(forms_client)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["ocr"] == {"status": "failed", "pages": [], "reason": "upstream_unavailable"}


def test_route_log_counts_ocr_and_proposals(stirling_ocr, caplog):
    import logging

    client, _, _ = stirling_ocr
    caplog.set_level(logging.INFO)
    _post(client, name="static-form.pdf")
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("forms_inspect"))
    assert "fields=0 detected=8 ocr=not_needed ocr_pages=0" in line
    for content in ("Bemerkungen", "Strasse", "Geburtsdatum"):
        assert content not in line


# --- OpenAPI contract ----------------------------------------------------------------------------


def _inspect_schema(client):
    spec = client.get("/openapi.json").json()
    schema = spec["paths"][INSPECT_URL]["post"]["responses"]["200"]["content"]["application/json"]["schema"]
    return {**schema, "components": spec["components"]}


@pytest.mark.parametrize("name, mode", [
    ("basic-form.pdf", None), ("static-form.pdf", None), ("scan-pages.pdf", "ocr"),
    ("signed-certified.pdf", None), ("encrypted-restricted.pdf", None), ("cropbox-offset.pdf", None),
    ("multi-widget-hierarchy.pdf", None), ("xfa-hybrid.pdf", None),
])
def test_responses_match_the_published_openapi_schema(stirling_ocr, name, mode):
    client, _, _ = stirling_ocr
    body = _post(client, name=name).json()
    schema = _inspect_schema(client)
    ref = schema["$ref"].split("/")[-1]
    resolver_schema = {"$ref": f"#/components/schemas/{ref}", "components": schema["components"]}
    jsonschema.validate(body, resolver_schema)


def test_committed_openapi_snapshot_is_current():
    from system.tools import export_forms_openapi

    committed = export_forms_openapi.DEFAULT_TARGET.read_text(encoding="utf-8")
    assert committed == export_forms_openapi.render(), (
        "docs/openapi/forms.openapi.json is outdated: "
        "run python -m system.tools.export_forms_openapi")


def test_snapshot_documents_request_options_and_error_statuses():
    from system.tools import export_forms_openapi

    spec = export_forms_openapi.build()
    op = spec["paths"][INSPECT_URL]["post"]
    assert sorted(op["responses"]) == ["200", "400", "401", "403", "413", "422"]
    body = spec["components"]["schemas"]["Body_forms_inspect_api_v1_documents_forms_inspect_post"]
    assert body["required"] == ["file"]
    assert body["properties"]["ocr"]["enum"] == ["auto", "off"]
    assert body["properties"]["detect"]["enum"] == ["auto", "off"]
    assert sorted(spec["paths"]) == ["/api/v1/documents/forms/fill", INSPECT_URL]  # only the forms contract


def test_error_responses_match_the_published_schema(forms_client):  # noqa: F811
    response = _post(forms_client, name="encrypted-user-password.pdf")
    spec = forms_client.get("/openapi.json").json()
    error = spec["paths"][INSPECT_URL]["post"]["responses"]["422"]["content"]["application/json"]["schema"]
    jsonschema.validate(response.json(), {**error, "components": spec["components"]})
