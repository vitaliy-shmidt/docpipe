"""Contract Import V2.1: POST /api/v1/documents/convert-to-pdf, the Stirling
conversion calls behind it, and the contract_document_classification mode."""

from __future__ import annotations

import httpx
import jsonschema
import pytest

from system.ai.modes import MODES
from system.config import StirlingSettings
from system.errors import DocPipeError
from system.services.stirling import CONVERSION_TARGETS, StirlingClient
from tests.conftest import AI_KEY, NO_DOCS_KEY, VALID_KEY, auth_headers

CONVERT_URL = "/api/v1/documents/convert-to-pdf"
CAPABILITIES_URL = "/api/v1/capabilities"

SAMPLES = {
    "docx": b"PK\x03\x04" + b"word/document.xml" + b"\x00" * 64,
    "odt": b"PK\x03\x04" + b"mimetypeapplication/vnd.oasis.opendocument.text",
    "doc": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64,
    "rtf": b"{\\rtf1\\ansi Wartungsvertrag}",
    "txt": "Wartungsvertrag zwischen A und B. Kündigung drei Monate.".encode("utf-8"),
    "jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 64,
    "jpeg": b"\xff\xd8\xff\xe0" + b"\x00" * 64,
    "png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    "tif": b"II*\x00" + b"\x00" * 64,
    "tiff": b"MM\x00*" + b"\x00" * 64,
}


def _convert(client, data: bytes, source_format: str, headers=None, filename: str = "upload.bin"):
    return client.post(
        CONVERT_URL,
        files={"file": (filename, data, "application/octet-stream")},
        data={"source_format": source_format},
        headers=auth_headers(VALID_KEY) if headers is None else headers,
    )


@pytest.mark.parametrize("fmt", sorted(SAMPLES))
def test_convert_each_supported_format_returns_pdf_bytes(client, fmt):
    response = _convert(client, SAMPLES[fmt], fmt)

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["x-docpipe-source-format"] == fmt
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"%PDF-")
    fake = client.fake_stirling["client"]
    assert fake.convert_calls[-1][1] == fmt


def test_convert_format_is_case_insensitive_and_trimmed(client):
    response = _convert(client, SAMPLES["png"], "  PNG ")
    assert response.status_code == 200
    assert client.fake_stirling["client"].convert_calls[-1][1] == "png"


@pytest.mark.parametrize("fmt", ["xlsx", "pptx", "zip", "pdf", "exe", "../docx"])
def test_convert_unsupported_format_rejected_before_stirling(client, fmt):
    response = _convert(client, b"PK\x03\x04data", fmt)

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_format"
    assert client.fake_stirling["client"].convert_calls == []


@pytest.mark.parametrize(
    "fmt,data",
    [
        ("docx", b"%PDF-1.4 not a docx"),
        ("doc", b"PK\x03\x04 not an ole file"),
        ("png", b"\xff\xd8\xff jpeg named png"),
        ("rtf", b"plain text named rtf"),
        ("tiff", b"\x89PNG\r\n\x1a\n"),
        ("txt", b"binary\x00content"),
    ],
)
def test_convert_magic_mismatch_is_invalid_file(client, fmt, data):
    response = _convert(client, data, fmt)

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file"
    assert client.fake_stirling["client"].convert_calls == []


def test_convert_missing_format_is_a_request_error(client):
    response = _convert(client, SAMPLES["docx"], "")
    assert response.status_code == 400
    assert client.fake_stirling["client"].convert_calls == []


def test_convert_empty_upload_is_invalid_file(client):
    response = _convert(client, b"", "txt")
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_file"


def test_convert_too_large_upload_rejected(client):
    # MAX_FILE_SIZE_MB = 1 in the test settings
    response = _convert(client, b"PK\x03\x04" + b"0" * (1024 * 1024 + 1), "docx")
    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"


def test_convert_requires_documents_service(client):
    response = _convert(client, SAMPLES["docx"], "docx", headers=auth_headers(NO_DOCS_KEY))
    assert response.status_code == 403
    assert response.json()["code"] == "service_disabled"


def test_convert_requires_auth(client):
    response = _convert(client, SAMPLES["docx"], "docx", headers={})
    assert response.status_code == 401


def test_convert_txt_is_sent_as_utf8_with_bom(client):
    _convert(client, "Kündigung".encode("utf-8"), "txt")
    sent = client.fake_stirling["client"].convert_calls[-1][0]
    assert sent == b"\xef\xbb\xbf" + "Kündigung".encode("utf-8")


def test_convert_legacy_cp1252_txt_is_reencoded(client):
    _convert(client, "Kündigung".encode("cp1252"), "txt")
    sent = client.fake_stirling["client"].convert_calls[-1][0]
    assert sent == b"\xef\xbb\xbf" + "Kündigung".encode("utf-8")


def test_convert_failure_is_conversion_failed_not_outage(client):
    client.fake_stirling["client"].convert_mode = "conversion_failed"
    response = _convert(client, SAMPLES["docx"], "docx")
    assert response.status_code == 422
    assert response.json()["code"] == "conversion_failed"


def test_convert_output_that_is_no_pdf_is_rejected(client):
    client.fake_stirling["client"].convert_mode = "not_pdf"
    response = _convert(client, SAMPLES["docx"], "docx")
    assert response.status_code == 422
    assert response.json()["code"] == "conversion_failed"


@pytest.mark.parametrize("mode,code,status", [("unavailable", "upstream_unavailable", 502), ("timeout", "timeout", 504)])
def test_convert_outage_codes_passed_through(client, mode, code, status):
    client.fake_stirling["client"].convert_mode = mode
    response = _convert(client, SAMPLES["png"], "png")
    assert response.status_code == status
    assert response.json()["code"] == code


def test_convert_log_has_no_file_name_or_content(client, caplog):
    caplog.set_level("INFO", logger="docpipe")
    _convert(client, "geheimer Vertragsinhalt".encode("utf-8"), "txt", filename="KONE geheim.txt")
    lines = [r.getMessage() for r in caplog.records if "convert_to_pdf" in r.getMessage()]
    assert lines and "source_format=txt" in lines[-1] and "status=success" in lines[-1]
    assert "geheim" not in " ".join(lines) and "KONE" not in " ".join(lines)


def test_capabilities_advertise_convert_to_pdf_for_documents_clients(client):
    body = client.get(CAPABILITIES_URL, headers=auth_headers(VALID_KEY)).json()
    assert "convert_to_pdf" in body["features"]
    body = client.get(CAPABILITIES_URL, headers=auth_headers(NO_DOCS_KEY)).json()
    assert "convert_to_pdf" not in body["features"]


# ---------------- StirlingClient.convert_to_pdf (request shape) ----------------


def _stirling(handler) -> StirlingClient:
    settings = StirlingSettings(base_url="http://stirling.test", api_key="secret-key", timeout_seconds=90)
    instance = StirlingClient(settings)
    instance._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return instance


def test_stirling_office_conversion_uses_verified_endpoint_and_server_file_name():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        captured["headers"] = request.headers
        return httpx.Response(200, content=b"%PDF-1.7 out", headers={"Content-Type": "application/pdf"})

    result = _stirling(handler).convert_to_pdf(SAMPLES["docx"], "docx")

    assert result == b"%PDF-1.7 out"
    assert captured["url"] == "http://stirling.test/api/v1/convert/file/pdf"
    assert captured["headers"]["X-API-KEY"] == "secret-key"
    assert b'name="fileInput"; filename="document.docx"' in captured["body"]
    assert b"wordprocessingml" in captured["body"]


def test_stirling_image_conversion_uses_verified_endpoint_and_options():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, content=b"%PDF-1.7 img", headers={"Content-Type": "application/pdf"})

    _stirling(handler).convert_to_pdf(SAMPLES["tiff"], "tiff")

    assert captured["url"] == "http://stirling.test/api/v1/convert/img/pdf"
    body = captured["body"]
    # TIFF is recognised by Stirling via the file name extension
    assert b'filename="document.tiff"' in body
    assert b"maintainAspectRatio" in body and b'name="colorType"' in body and b'name="autoRotate"' in body


@pytest.mark.parametrize("status,code", [(500, "conversion_failed"), (400, "conversion_failed"), (502, "upstream_unavailable"), (503, "upstream_unavailable"), (401, "upstream_auth_failed")])
def test_stirling_conversion_error_mapping(status, code):
    client = _stirling(lambda request: httpx.Response(status, text="raw stirling error"))
    with pytest.raises(DocPipeError) as exc:
        client.convert_to_pdf(SAMPLES["png"], "png")
    assert exc.value.code == code
    assert "raw stirling error" not in exc.value.message


def test_stirling_conversion_connection_error_is_outage():
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(DocPipeError) as exc:
        _stirling(handler).convert_to_pdf(SAMPLES["png"], "png")
    assert exc.value.code == "upstream_unavailable"


def test_extract_text_path_keeps_its_500_as_outage():
    client = _stirling(lambda request: httpx.Response(500, text="x"))
    with pytest.raises(DocPipeError) as exc:
        client.extract_text(b"%PDF-1.4", "document.pdf")
    assert exc.value.code == "upstream_unavailable"


def test_conversion_targets_cover_exactly_the_supported_formats():
    assert set(CONVERSION_TARGETS) == {"docx", "doc", "odt", "rtf", "txt", "jpg", "jpeg", "png", "tif", "tiff"}


# ---------------- contract_document_classification ----------------

CLASSIFICATION = MODES["contract_document_classification"]


def test_classification_mode_is_small_and_light():
    assert CLASSIFICATION.model_profile == "light"
    assert CLASSIFICATION.max_input_length == 20_000
    assert set(CLASSIFICATION.response_schema["properties"]) == {"classification", "document_kind", "reason_code"}


@pytest.mark.parametrize("value", ["contract", "uncertain", "not_contract"])
def test_classification_schema_accepts_the_three_classes(value):
    jsonschema.validate(
        {"classification": value, "document_kind": "service_contract", "reason_code": "contract_terms_detected"},
        CLASSIFICATION.response_schema,
    )


@pytest.mark.parametrize(
    "result",
    [
        {"classification": "maybe", "document_kind": "invoice", "reason_code": "invoice_detected"},
        {"classification": "contract", "document_kind": "invoice", "reason_code": "because the text says so"},
        {"classification": "contract", "document_kind": "invoice", "reason_code": "ambiguous", "vendor_name": "KONE"},
        {"classification": "contract"},
    ],
)
def test_classification_schema_rejects_free_text_and_extra_fields(result):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(result, CLASSIFICATION.response_schema)


def test_classification_prompt_prefers_uncertain_and_extracts_nothing():
    prompt = (CLASSIFICATION_PROMPT_PATH).read_text(encoding="utf-8")
    assert "When in doubt, ALWAYS choose \"uncertain\"" in prompt
    assert "do NOT extract" in prompt
    assert "{context_block}" in prompt and "{text}" in prompt


def test_classification_runs_through_analyze_with_light_profile(client):
    fake = client.fake_ollama["client"]
    fake.result = {"classification": "not_contract", "document_kind": "invoice", "reason_code": "invoice_detected"}
    response = client.post(
        "/api/v1/documents/analyze",
        json={"mode": "contract_document_classification", "context": {"document_type_target": "contract"}, "text": "RECHNUNG Nr. 1"},
        headers=auth_headers(AI_KEY),
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["model_profile"] == "light"
    assert data["result"]["classification"] == "not_contract"
    assert "RECHNUNG Nr. 1" in fake.last_prompt


from system.ai.modes import PROMPTS_DIR  # noqa: E402

CLASSIFICATION_PROMPT_PATH = PROMPTS_DIR / "contract_document_classification" / "v1.txt"
