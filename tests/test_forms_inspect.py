"""Phase 2.1a: services.forms and POST /api/v1/documents/forms/inspect.

Reads the committed synthetic reference PDFs in tests/fixtures/forms/
(generator: tests/forms_fixtures.py). Expected positions are hand-computed
from the generator's user-space rectangles:

    visible page = CropBox ∩ MediaBox, origin top-left, page from 1,
    /Rotate (clockwise) applied; field rect (120, 750, 320, 770) on A4
    (595 x 842 pt) => rotation 0: x 120, y 842-770 = 72, 200 x 20.
"""

from __future__ import annotations

import hashlib
import logging
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from system.config import ClientConfig, ClientServices, ConfigError, FormsSettings
from system.forms.geometry import PageBox
from system.forms.inspect import inspect_pdf
from tests.conftest import AI_KEY, VALID_KEY, auth_headers
from tests.forms_fixtures import FIXTURE_DIR, OWNER_PASSWORD, REFERENCE_PDFS, USER_PASSWORD

INSPECT_URL = "/api/v1/documents/forms/inspect"
CAPABILITIES_URL = "/api/v1/capabilities"
FORMS_KEY = "forms-key"
FORMS_ONLY_KEY = "forms-only-key"
LIMITS = {"max_pages": 200, "max_fields": 2000}


def fixture(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def inspect(name: str, password: str | None = None, **limits) -> dict:
    return inspect_pdf(fixture(name), password, **{**LIMITS, **limits})


def by_name(data: dict, input_name: str) -> list[dict]:
    return [f for f in data["fields"] if f["input_name"] == input_name]


def codes(data: dict) -> list[str]:
    return [w["code"] for w in data["warnings"]]


@pytest.fixture
def forms_client(client):
    """The shared app client plus two forms-enabled clients (documents+forms, forms only)."""
    settings = client.app.state.settings
    extra = {
        "forms-client": ClientConfig(client_id="forms-client", enabled=True, api_key=FORMS_KEY,
                                     services=ClientServices(documents=True, forms=True)),
        "forms-only-client": ClientConfig(client_id="forms-only-client", enabled=True, api_key=FORMS_ONLY_KEY,
                                          services=ClientServices(forms=True)),
    }
    client.app.state.settings = replace(settings, clients={**settings.clients, **extra})
    return client


def post_inspect(client, content: bytes, key: str = FORMS_KEY, password: str | None = None):
    data = {"password": password} if password is not None else None
    files = {"file": ("x.pdf", content, "application/pdf")}
    return client.post(INSPECT_URL, headers=auth_headers(key), files=files, data=data)


# --- reference files -----------------------------------------------------------------------


def test_every_reference_pdf_is_committed():
    assert sorted(p.name for p in FIXTURE_DIR.glob("*.pdf")) == sorted(REFERENCE_PDFS)


# --- native fields ---------------------------------------------------------------------------


def test_basic_form_fields_types_and_attributes():
    data = inspect("basic-form.pdf")
    assert data["schema_version"] == "1"
    assert data["sha256"] == hashlib.sha256(fixture("basic-form.pdf")).hexdigest()
    assert data["page_count"] == 1

    [vorname] = by_name(data, "vorname")
    assert vorname["name"] == "Vorname"  # /TU wins over the technical name
    assert vorname["type"] == "text" and vorname["source"] == "native"
    assert vorname["required"] is True and vorname["max_length"] == 40 and vorname["multiline"] is False
    assert vorname["page"] == 1
    assert vorname["position"] == {"x": 120.0, "y": 72.0, "width": 200.0, "height": 20.0}
    assert vorname["widget_count"] == 1 and vorname["native_ref"] and vorname["field_ref"]

    [bemerkung] = by_name(data, "bemerkung")
    assert bemerkung["multiline"] is True and bemerkung["name"] == "bemerkung"

    [box] = by_name(data, "einverstanden")
    assert box["type"] == "checkbox" and box["export_value"] == "Ja"
    assert box["options"] == [{"value": "Ja", "label": "Ja"}]

    radios = by_name(data, "anrede")
    assert [r["type"] for r in radios] == ["radio", "radio"]
    assert [r["export_value"] for r in radios] == ["frau", "herr"]
    assert radios[0]["field_ref"] == radios[1]["field_ref"]
    assert radios[0]["native_ref"] != radios[1]["native_ref"]
    assert [r["widget_index"] for r in radios] == [0, 1]
    assert radios[0]["options"] == [{"value": "frau", "label": "frau"}, {"value": "herr", "label": "herr"}]

    [land] = by_name(data, "land")
    assert land["type"] == "choice" and land["choice_style"] == "combo" and land["multi_select"] is False
    assert land["options"] == [
        {"value": "DE", "label": "Deutschland"}, {"value": "AT", "label": "Österreich"}]

    assert not by_name(data, "drucken")
    assert {"code": "push_buttons_ignored", "message": "Push buttons cannot be filled and were ignored.",
            "count": 1} in data["warnings"]


def test_field_ids_are_sequential_in_reading_order():
    data = inspect("basic-form.pdf")
    assert [f["id"] for f in data["fields"]] == [f"field_{n:03d}" for n in range(1, 7)]
    assert [f["input_name"] for f in data["fields"]] == [
        "vorname", "einverstanden", "anrede", "anrede", "land", "bemerkung"]


def test_inspection_is_deterministic():
    assert inspect("basic-form.pdf") == inspect("basic-form.pdf")


# --- geometry: rotation, CropBox, inheritance -------------------------------------------------


@pytest.mark.parametrize(
    "rotation, page, size, position",
    [
        (0, 1, (595.0, 842.0), {"x": 120.0, "y": 72.0, "width": 200.0, "height": 20.0}),
        (90, 2, (842.0, 595.0), {"x": 750.0, "y": 120.0, "width": 20.0, "height": 200.0}),
        (180, 3, (595.0, 842.0), {"x": 275.0, "y": 750.0, "width": 200.0, "height": 20.0}),
        (270, 4, (842.0, 595.0), {"x": 72.0, "y": 275.0, "width": 20.0, "height": 200.0}),
    ],
)
def test_rotated_pages_are_normalised_to_the_displayed_page(rotation, page, size, position):
    data = inspect("rotated-pages.pdf")
    p = data["pages"][page - 1]
    assert (p["width"], p["height"], p["rotation"]) == (*size, rotation)
    [field] = by_name(data, f"vorname_r{rotation}")
    assert field["page"] == page and field["position"] == position


@pytest.mark.parametrize("rotation, page", [(0, 1), (90, 2), (180, 3), (270, 4)])
def test_text_blocks_share_the_field_coordinate_system(rotation, page):
    """The label sits left of the field (user x 50..~95, baseline 760) on every page; after
    normalisation it must keep the same relation to the field as on screen."""
    data = inspect("rotated-pages.pdf")
    [block] = [b for b in data["pages"][page - 1]["text_blocks"] if b["text"] == "Vorname:"]
    [field] = by_name(data, f"vorname_r{rotation}")
    b, f = block["position"], field["position"]
    if rotation == 0:
        assert b["x"] == pytest.approx(50, abs=1) and b["x"] + b["width"] < f["x"]
        assert f["y"] <= b["y"] <= f["y"] + f["height"]
    elif rotation == 90:  # the reading direction runs top -> bottom
        assert b["y"] == pytest.approx(50, abs=1) and b["y"] + b["height"] < f["y"]
        assert f["x"] <= b["x"] + b["width"] / 2 <= f["x"] + f["width"]
    elif rotation == 180:
        assert b["x"] + b["width"] == pytest.approx(595 - 50, abs=1) and f["x"] + f["width"] < b["x"]
    else:  # 270: bottom -> top; y comes from the UNrotated width (595): 595 - user x
        assert b["y"] + b["height"] == pytest.approx(595 - 50, abs=1) and f["y"] + f["height"] < b["y"]


def test_cropbox_and_mediabox_origin_define_the_visible_page():
    data = inspect("cropbox-offset.pdf")
    page = data["pages"][0]
    assert (page["width"], page["height"]) == (535.0, 762.0)  # CropBox 40..575 x 60..822
    [name] = by_name(data, "name")
    assert name["position"] == {"x": 60.0, "y": 22.0, "width": 200.0, "height": 20.0}
    [clipped] = by_name(data, "ragt_heraus")
    assert clipped["position"] == {"x": 460.0, "y": 702.0, "width": 75.0, "height": 20.0}
    assert not by_name(data, "ausserhalb")
    assert "widget_clipped" in codes(data)
    assert {"code": "widgets_outside_page_ignored", "count": 1}.items() <= next(
        w for w in data["warnings"] if w["code"] == "widgets_outside_page_ignored").items()
    [label] = [b for b in page["text_blocks"] if b["text"] == "Name"]
    assert label["position"]["x"] == pytest.approx(20, abs=1)  # user x 60 - CropBox 40


def test_multi_widget_field_hierarchical_name_inherited_rotation_and_hidden_widget():
    data = inspect("multi-widget-hierarchy.pdf")
    assert [p["rotation"] for p in data["pages"]] == [90, 90, 90]  # inherited from /Pages
    widgets = by_name(data, "person.name")
    assert [(w["page"], w["widget_index"], w["widget_count"]) for w in widgets] == [(1, 0, 2), (2, 1, 2)]
    assert widgets[0]["field_ref"] == widgets[1]["field_ref"]
    assert widgets[1]["position"] == {"x": 390.0, "y": 300.0, "width": 20.0, "height": 200.0}
    [hidden] = by_name(data, "intern")
    assert hidden["hidden"] is True and widgets[0]["hidden"] is False


def test_invalid_rotation_is_treated_as_zero_with_a_warning():
    data = inspect("invalid-rotation.pdf")
    assert data["pages"][0]["rotation"] == 0
    assert {"code": "invalid_page_rotation", "page": 1}.items() <= data["warnings"][0].items()


def test_pagebox_clip_reports_partial_and_full_overlap():
    import pikepdf

    pdf = pikepdf.new()
    page = pdf.add_blank_page(page_size=(100, 100))
    box = PageBox.from_page(page.obj)
    inside, clipped = box.clip(box.to_visible(10, 10, 20, 20))
    assert not clipped and inside.as_dict() == {"x": 10.0, "y": 80.0, "width": 10.0, "height": 10.0}
    assert box.clip(box.to_visible(200, 200, 210, 210)) == (None, True)


# --- text --------------------------------------------------------------------------------------


def test_page_text_and_text_source():
    data = inspect("static-no-fields.pdf")
    assert data["fields"] == []
    assert [p["text_source"] for p in data["pages"]] == ["embedded", "none"]
    assert data["pages"][0]["text"] == "Antrag ohne Formularfelder"
    assert data["text"] == "Antrag ohne Formularfelder"
    assert {"code": "no_text_layer", "page": 2}.items() <= next(
        w for w in data["warnings"] if w["code"] == "no_text_layer").items()


def test_document_text_joins_pages():
    data = inspect("five-pages.pdf")
    assert data["text"] == "\n\n".join(f"Seite {n}" for n in range(1, 6))


# --- security: encryption, permissions, signatures, XFA ----------------------------------------


def test_open_password_required_and_invalid_password():
    from system.errors import DocPipeError

    with pytest.raises(DocPipeError) as missing:
        inspect("encrypted-user-password.pdf")
    assert missing.value.code == "password_required" and missing.value.status_code == 422
    with pytest.raises(DocPipeError) as wrong:
        inspect("encrypted-user-password.pdf", "wrong")
    assert wrong.value.code == "invalid_password"
    assert "wrong" not in wrong.value.message


def test_correct_password_opens_and_reports_encryption():
    data = inspect("encrypted-user-password.pdf", USER_PASSWORD)
    security = data["security"]
    assert security["encrypted"] is True and security["open_password_required"] is True
    assert security["encryption_method"] == "aesv3"
    assert security["permissions"]["fill_forms"] is True
    assert by_name(data, "feld") and data["pages"][0]["text"] == "Geheim"


def test_owner_restrictions_are_respected_not_bypassed():
    data = inspect("encrypted-restricted.pdf")
    security = data["security"]
    assert security["encrypted"] is True and security["open_password_required"] is False
    assert security["permissions"]["fill_forms"] is False
    assert security["permissions"]["extract_text"] is False
    assert data["text"] == "" and data["pages"][0]["text_blocks"] == []
    assert {"form_filling_not_permitted", "text_extraction_not_permitted"} <= set(codes(data))
    assert "no_text_layer" not in codes(data)


def test_owner_password_grants_full_permissions():
    data = inspect("encrypted-restricted.pdf", OWNER_PASSWORD)
    assert data["security"]["permissions"]["fill_forms"] is True
    assert data["security"]["open_password_required"] is False  # the file opens without one
    assert data["pages"][0]["text"] == "Nur lesen"


def test_unencrypted_pdf_has_no_permissions_block():
    security = inspect("basic-form.pdf")["security"]
    assert security == {"encrypted": False, "open_password_required": False, "encryption_method": None,
                        "permissions": None, "signed": False, "certified": False, "xfa": False}


def test_signatures_and_certification_are_reported():
    data = inspect("signed-certified.pdf")
    signed, unsigned = by_name(data, "unterschrift")[0], by_name(data, "zweitunterschrift")[0]
    assert signed["type"] == "signature" and signed["signed"] is True and unsigned["signed"] is False
    assert data["security"]["signed"] is True and data["security"]["certified"] is True
    assert {"signed_document", "certified_document"} <= set(codes(data))


def test_hybrid_xfa_reports_acroform_fields_with_warning():
    data = inspect("xfa-hybrid.pdf")
    assert data["security"]["xfa"] is True and by_name(data, "feld")
    assert "xfa_present" in codes(data)


def test_dynamic_xfa_is_rejected():
    from system.errors import DocPipeError

    with pytest.raises(DocPipeError) as exc:
        inspect("xfa-dynamic.pdf")
    assert exc.value.code == "xfa_unsupported" and exc.value.status_code == 422


# --- limits and broken input ------------------------------------------------------------------


@pytest.mark.parametrize("limits", [{"max_pages": 4}, {"max_fields": 5}])
def test_limits(limits):
    from system.errors import DocPipeError

    name = "five-pages.pdf" if "max_pages" in limits else "basic-form.pdf"
    with pytest.raises(DocPipeError) as exc:
        inspect(name, **limits)
    assert exc.value.code == "limit_exceeded" and exc.value.status_code == 413


def test_limits_at_the_boundary_pass():
    assert inspect("five-pages.pdf", max_pages=5)["page_count"] == 5
    assert len(inspect("basic-form.pdf", max_fields=6)["fields"]) == 6


def test_unreadable_pdf_is_invalid_file():
    from system.errors import DocPipeError

    with pytest.raises(DocPipeError) as exc:
        inspect_pdf(b"%PDF-1.7\n this is not a pdf body", None, **LIMITS)
    assert exc.value.code == "invalid_file"


def test_damaged_but_recoverable_pdf_is_flagged():
    damaged = fixture("basic-form.pdf").replace(b"startxref", b"startxrfe")
    data = inspect_pdf(damaged, None, **LIMITS)
    assert "pdf_structure_repaired" in codes(data) and by_name(data, "vorname")


# --- route, permissions, capabilities -----------------------------------------------------------


def test_route_requires_forms_flag(forms_client):
    response = post_inspect(forms_client, fixture("basic-form.pdf"), key=VALID_KEY)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "code": "forms_disabled",
                               "message": "The forms service is not enabled for this client."}
    assert post_inspect(forms_client, fixture("basic-form.pdf"), key="unknown").status_code == 401


@pytest.mark.parametrize("key", [FORMS_KEY, FORMS_ONLY_KEY])
def test_route_success(forms_client, key):
    response = post_inspect(forms_client, fixture("basic-form.pdf"), key=key)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ok"] is True
    assert body["data"] == inspect("basic-form.pdf")


def test_route_password_handling_and_no_leak(forms_client, caplog):
    caplog.set_level(logging.INFO)
    missing = post_inspect(forms_client, fixture("encrypted-user-password.pdf"))
    assert missing.status_code == 422 and missing.json()["code"] == "password_required"
    wrong = post_inspect(forms_client, fixture("encrypted-user-password.pdf"), password="falsch-123")
    assert wrong.status_code == 422 and wrong.json()["code"] == "invalid_password"
    ok = post_inspect(forms_client, fixture("encrypted-user-password.pdf"), password=USER_PASSWORD)
    assert ok.status_code == 200 and ok.json()["data"]["security"]["open_password_required"] is True
    for secret in (USER_PASSWORD, "falsch-123"):
        assert secret not in caplog.text
        assert secret not in wrong.text and secret not in ok.text
    # Metadata only: no field names or page text in the log.
    assert "forms_inspect" in caplog.text and "status=invalid_password" in caplog.text
    assert "Geheim" not in caplog.text and "feld" not in caplog.text.replace("fields=", "")


def test_route_log_contains_no_document_content(forms_client, caplog):
    caplog.set_level(logging.INFO)
    assert post_inspect(forms_client, fixture("basic-form.pdf")).status_code == 200
    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("forms_inspect"))
    assert "pages=1 fields=6" in line and "status=success" in line
    for content in ("Vorname", "vorname", "Einverstanden", "Österreich", "anrede"):
        assert content not in line


@pytest.mark.parametrize(
    "content, code, status",
    [
        (b"not a pdf at all", "invalid_file", 400),
        (b"", "invalid_file", 400),
        (b"%PDF-1.7\n garbage", "invalid_file", 400),
    ],
)
def test_route_rejects_bad_uploads(forms_client, content, code, status):
    response = post_inspect(forms_client, content)
    assert response.status_code == status and response.json()["code"] == code


def test_route_missing_file_is_invalid_file(forms_client):
    response = forms_client.post(INSPECT_URL, headers=auth_headers(FORMS_KEY))
    assert response.status_code == 400 and response.json()["code"] == "invalid_file"


def test_route_enforces_upload_size_limit(forms_client):
    too_big = b"%PDF-1.7\n" + b"0" * (1024 * 1024 + 1)  # test server limit is 1 MB
    response = post_inspect(forms_client, too_big)
    assert response.status_code == 413 and response.json()["code"] == "file_too_large"


def test_route_uses_configured_limits(forms_client):
    settings = forms_client.app.state.settings
    forms_client.app.state.settings = replace(settings, forms=FormsSettings(max_pages=2, max_fields=10))
    response = post_inspect(forms_client, fixture("five-pages.pdf"))
    assert response.status_code == 413 and response.json()["code"] == "limit_exceeded"


def test_route_removes_temp_files(forms_client, tmp_path, monkeypatch):
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    post_inspect(forms_client, fixture("basic-form.pdf"))
    post_inspect(forms_client, fixture("encrypted-user-password.pdf"))
    assert list(tmp_path.iterdir()) == []


def test_capabilities_unchanged_for_clients_without_forms(forms_client):
    """Exact, complete bodies: existing clients (e.g. HubDix) must not see any new key."""
    body = forms_client.get(CAPABILITIES_URL, headers=auth_headers(VALID_KEY)).json()
    assert body == {
        "ok": True,
        "services": {"documents": True, "ai": False, "prompt_lab": False, "assistant": False},
        "features": ["extract_text", "convert_to_pdf", "ocr_fallback"],
        "ai_modes": None,
        "assistant_modes": None,
        "assistant_domains": None,
    }
    ai_body = forms_client.get(CAPABILITIES_URL, headers=auth_headers(AI_KEY)).json()
    assert "forms" not in ai_body and "forms" not in ai_body["services"]
    assert "forms_inspect" not in ai_body["features"]


def test_capabilities_for_forms_clients(forms_client):
    body = forms_client.get(CAPABILITIES_URL, headers=auth_headers(FORMS_KEY)).json()
    assert body["services"] == {"documents": True, "ai": False, "prompt_lab": False, "assistant": False,
                                "forms": True}
    assert body["features"] == ["extract_text", "convert_to_pdf", "ocr_fallback", "forms_inspect"]
    assert body["forms"] == {"schema_version": "1", "max_pages": 200, "max_fields": 2000}
    only = forms_client.get(CAPABILITIES_URL, headers=auth_headers(FORMS_ONLY_KEY)).json()
    assert only["services"]["documents"] is False and only["features"] == ["forms_inspect"]


def test_existing_document_endpoints_ignore_the_forms_flag(forms_client):
    """forms does not grant documents: a forms-only client cannot use extract-text."""
    response = forms_client.post("/api/v1/documents/extract-text", headers=auth_headers(FORMS_ONLY_KEY),
                                 files={"file": ("x.pdf", fixture("basic-form.pdf"), "application/pdf")})
    assert response.status_code == 403 and response.json()["code"] == "service_disabled"


# --- configuration ----------------------------------------------------------------------------


def _load(tmp_path: Path, monkeypatch, yaml_text: str):
    from system.config import load_settings

    path = tmp_path / "config.yaml"
    path.write_text(textwrap.dedent(yaml_text), encoding="utf-8")
    monkeypatch.setenv("DOCPIPE_CONFIG", str(path))
    return load_settings()


def test_config_forms_flag_defaults_to_false(tmp_path, monkeypatch):
    settings = _load(tmp_path, monkeypatch, """
        clients:
          legacy:
            enabled: true
            api_key: k1
            services: {documents: true}
          pdf4fill:
            enabled: true
            api_key: k2
            services: {documents: true, forms: true}
    """)
    assert settings.clients["legacy"].services.forms is False
    assert settings.clients["pdf4fill"].services.forms is True
    assert settings.forms == FormsSettings()


def test_config_forms_limits(tmp_path, monkeypatch):
    settings = _load(tmp_path, monkeypatch, "forms: {max_pages: 50, max_fields: 300}\n")
    assert settings.forms == FormsSettings(max_pages=50, max_fields=300)


@pytest.mark.parametrize("block", ["forms: 5", "forms: {max_pages: 0}", "forms: {max_pages: 'many'}",
                                   "forms: {max_fields: true}", "forms: {unknown: 1}"])
def test_config_forms_validation(tmp_path, monkeypatch, block):
    with pytest.raises(ConfigError):
        _load(tmp_path, monkeypatch, block + "\n")
