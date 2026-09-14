from __future__ import annotations

import glob
import os
import tempfile
from dataclasses import replace

from tests.conftest import DISABLED_KEY, NO_DOCS_KEY, VALID_KEY, auth_headers, pdf_bytes

EXTRACT_URL = "/api/v1/documents/extract-text"
CAPABILITIES_URL = "/api/v1/capabilities"


def _upload(client, filename="test.pdf", content=None, content_type="application/pdf", headers=None):
    content = pdf_bytes() if content is None else content
    return client.post(
        EXTRACT_URL,
        files={"file": (filename, content, content_type)},
        headers=auth_headers(VALID_KEY) if headers is None else headers,
    )


def test_health_ok_without_auth(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "service": "docpipe", "version": "0.1.0"}


def test_capabilities_missing_auth(client):
    response = client.get(CAPABILITIES_URL)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_capabilities_invalid_key(client):
    response = client.get(CAPABILITIES_URL, headers=auth_headers("does-not-exist"))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_capabilities_disabled_client(client):
    response = client.get(CAPABILITIES_URL, headers=auth_headers(DISABLED_KEY))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_capabilities_valid_key(client):
    response = client.get(CAPABILITIES_URL, headers=auth_headers(VALID_KEY))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["services"] == {"documents": True, "ai": False, "prompt_lab": False, "assistant": False}
    assert "extract_text" in body["features"]
    # OCR is on by default (see config.py DEFAULT_OCR_ENABLED) - the test
    # settings don't override it, so it must be advertised too.
    assert "ocr_fallback" in body["features"]


def test_capabilities_hides_ocr_fallback_when_disabled(client):
    settings = client.app.state.settings
    client.app.state.settings = replace(
        settings, stirling=replace(settings.stirling, ocr=replace(settings.stirling.ocr, enabled=False))
    )

    response = client.get(CAPABILITIES_URL, headers=auth_headers(VALID_KEY))

    assert response.status_code == 200
    assert "ocr_fallback" not in response.json()["features"]


def test_extract_text_missing_auth(client):
    response = _upload(client, headers={})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_extract_text_invalid_key(client):
    response = _upload(client, headers=auth_headers("wrong-key"))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_extract_text_disabled_client(client):
    response = _upload(client, headers=auth_headers(DISABLED_KEY))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_extract_text_service_disabled_for_client(client):
    response = _upload(client, headers=auth_headers(NO_DOCS_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "service_disabled"


def test_extract_text_invalid_mime(client):
    response = _upload(client, content=b"this is not a pdf")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file"


def test_extract_text_file_too_large(client):
    too_large = pdf_bytes(1_100_000)  # server test settings cap at 1 MB
    response = _upload(client, content=too_large)
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_extract_text_success(client):
    fake = client.fake_stirling["client"]
    response = _upload(client)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["text"] == fake.text
    assert body["data"]["text_length"] == len(fake.text)
    assert body["data"]["extraction_method"] == "embedded_text"
    # Normal, text-based PDF: OCR must never be invoked.
    assert fake.extract_calls == 1
    assert fake.ocr_calls == 0


def test_extract_text_stirling_unavailable(client):
    fake = client.fake_stirling["client"]
    fake.mode = "unavailable"
    response = _upload(client)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_unavailable"
    # A technical failure of the first extraction must never trigger OCR.
    assert fake.ocr_calls == 0


def test_extract_text_stirling_timeout(client):
    fake = client.fake_stirling["client"]
    fake.mode = "timeout"
    response = _upload(client)
    assert response.status_code == 504
    assert response.json()["code"] == "timeout"
    assert fake.ocr_calls == 0


def test_extract_text_stirling_auth_failed(client):
    fake = client.fake_stirling["client"]
    fake.mode = "auth_failed"
    response = _upload(client)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_auth_failed"
    assert fake.ocr_calls == 0


# --- OCR fallback ------------------------------------------------------------


def test_extract_text_scanned_pdf_triggers_ocr(client):
    """First extraction technically succeeds but comes back with only a
    handful of characters (e.g. scan artifacts) - OCR must run exactly
    once, and the OCR'd PDF's text must be re-extracted and returned."""
    fake = client.fake_stirling["client"]
    fake.text = "3"
    fake.second_extract_text = "This is the real text recovered via OCR."

    response = _upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["text"] == fake.second_extract_text
    assert body["data"]["extraction_method"] == "ocr"
    assert fake.ocr_calls == 1
    assert fake.extract_calls == 2


def test_extract_text_whitespace_only_triggers_ocr(client):
    fake = client.fake_stirling["client"]
    fake.text = "\n \t \n"

    response = _upload(client)

    assert response.status_code == 200
    assert response.json()["data"]["extraction_method"] == "ocr"
    assert fake.ocr_calls == 1


def test_extract_text_short_real_text_also_triggers_ocr(client):
    """Not every short text is a scan, but the V1 heuristic is deliberately
    pragmatic: a short-but-real document (e.g. just "OK") still falls below
    the threshold and triggers an OCR attempt. This is accepted, not a bug.
    """
    fake = client.fake_stirling["client"]
    fake.text = "OK"
    fake.second_extract_text = "OK"

    response = _upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["extraction_method"] == "ocr"
    assert fake.ocr_calls == 1


def test_extract_text_ocr_call_itself_fails(client):
    """First extraction succeeds but is insufficient; Stirling's OCR
    endpoint itself times out - the error must be normalized like any
    other upstream timeout, not silently swallowed into a short-text
    success."""
    fake = client.fake_stirling["client"]
    fake.text = "3"
    fake.ocr_mode = "timeout"

    response = _upload(client)

    assert response.status_code == 504
    assert response.json()["code"] == "timeout"
    assert fake.ocr_calls == 1


def test_extract_text_second_extraction_after_ocr_fails(client):
    """OCR itself produces a PDF successfully, but the follow-up text
    extraction on that OCR'd PDF fails - must surface as a normal
    normalized upstream error."""
    fake = client.fake_stirling["client"]
    fake.text = "3"
    fake.second_extract_mode = "unavailable"

    response = _upload(client)

    assert response.status_code == 502
    assert response.json()["code"] == "upstream_unavailable"
    assert fake.ocr_calls == 1
    assert fake.extract_calls == 2


def test_extract_text_ocr_success_but_no_text_found_is_still_ok(client):
    """OCR technically succeeds (a real OCR attempt was made) but the page
    turns out to be a blank scan / pure image with nothing recognizable -
    that is a successful response with empty text, not an error."""
    fake = client.fake_stirling["client"]
    fake.text = "3"
    fake.second_extract_text = ""

    response = _upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["text"] == ""
    assert body["data"]["text_length"] == 0
    assert body["data"]["extraction_method"] == "ocr"


def test_extract_text_ocr_disabled_skips_fallback_even_for_short_text(client):
    settings = client.app.state.settings
    client.app.state.settings = replace(
        settings, stirling=replace(settings.stirling, ocr=replace(settings.stirling.ocr, enabled=False))
    )
    fake = client.fake_stirling["client"]
    fake.text = "3"

    response = _upload(client)

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["extraction_method"] == "embedded_text"
    assert body["data"]["text"] == "3"
    assert fake.ocr_calls == 0


def test_no_leftover_temp_files_after_requests(client):
    pattern = os.path.join(tempfile.gettempdir(), "docpipe_*")
    before = set(glob.glob(pattern))

    _upload(client)  # success path (embedded text)
    _upload(client, content=b"not a pdf")  # error path

    fake = client.fake_stirling["client"]
    fake.text = "3"
    _upload(client)  # success path via OCR fallback

    fake.ocr_mode = "timeout"
    _upload(client)  # OCR failure path

    fake.ocr_mode = "success"
    fake.second_extract_mode = "unavailable"
    _upload(client)  # second-extraction failure path

    after = set(glob.glob(pattern))
    assert after == before
