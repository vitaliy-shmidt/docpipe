from __future__ import annotations

import glob
import os
import tempfile

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
    assert body["services"] == {"documents": True, "ai": False}
    assert "extract_text" in body["features"]


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
    response = _upload(client)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["data"]["text"] == "extracted text"
    assert body["data"]["text_length"] == len("extracted text")


def test_extract_text_stirling_unavailable(client):
    client.fake_stirling["client"].mode = "unavailable"
    response = _upload(client)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_unavailable"


def test_extract_text_stirling_timeout(client):
    client.fake_stirling["client"].mode = "timeout"
    response = _upload(client)
    assert response.status_code == 504
    assert response.json()["code"] == "timeout"


def test_extract_text_stirling_auth_failed(client):
    client.fake_stirling["client"].mode = "auth_failed"
    response = _upload(client)
    assert response.status_code == 502
    assert response.json()["code"] == "upstream_auth_failed"


def test_no_leftover_temp_files_after_requests(client):
    pattern = os.path.join(tempfile.gettempdir(), "docpipe_*")
    before = set(glob.glob(pattern))

    _upload(client)  # success path
    _upload(client, content=b"not a pdf")  # error path

    after = set(glob.glob(pattern))
    assert after == before
