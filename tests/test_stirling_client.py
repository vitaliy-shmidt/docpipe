from __future__ import annotations

import httpx
import pytest

from system.config import StirlingSettings
from system.errors import DocPipeError
from system.services.stirling import StirlingClient


def _make_client(handler, timeout_seconds: int = 90) -> StirlingClient:
    settings = StirlingSettings(
        base_url="http://stirling.test", api_key="secret-key", timeout_seconds=timeout_seconds
    )
    client = StirlingClient(settings)
    client._client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return client


def test_extract_text_sends_expected_request_shape():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        return httpx.Response(200, text="hello world")

    client = _make_client(handler)
    text = client.extract_text(b"%PDF-1.4 ...", "document.pdf")

    assert text == "hello world"
    assert captured["url"] == "http://stirling.test/api/v1/convert/pdf/text"
    assert captured["headers"]["X-API-KEY"] == "secret-key"


def test_ocr_pdf_sends_expected_endpoint_fields_and_languages():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = request.headers
        body = request.content.decode("utf-8", errors="replace")
        captured["body"] = body
        return httpx.Response(
            200, content=b"%PDF-1.4 ocr-output", headers={"Content-Type": "application/pdf"}
        )

    client = _make_client(handler)
    result = client.ocr_pdf(b"%PDF-1.4 ...", "document.pdf", ["deu", "eng"])

    assert result == b"%PDF-1.4 ocr-output"
    assert captured["url"] == "http://stirling.test/api/v1/misc/ocr-pdf"
    assert captured["headers"]["X-API-KEY"] == "secret-key"
    # Multipart field name and repeated `languages` entries (one per
    # language, not a single "deu+eng" combined value) - verified against
    # the Stirling-PDF 2.14.2 OCRController/ProcessPdfWithOcrRequest source.
    assert 'name="fileInput"' in captured["body"]
    assert captured["body"].count('name="languages"') == 2
    assert 'name="ocrType"' in captured["body"]
    assert "skip-text" in captured["body"]
    assert 'name="ocrRenderType"' in captured["body"]
    assert "hocr" in captured["body"]


@pytest.mark.parametrize(
    "status_code,expected_code",
    [
        (401, "upstream_auth_failed"),
        (403, "upstream_auth_failed"),
        (500, "upstream_unavailable"),
        (400, "processing_failed"),
    ],
)
def test_ocr_pdf_maps_http_status_to_normalized_errors(status_code, expected_code):
    client = _make_client(lambda request: httpx.Response(status_code))

    with pytest.raises(DocPipeError) as exc_info:
        client.ocr_pdf(b"%PDF-1.4 ...", "document.pdf", ["deu"])

    assert exc_info.value.code == expected_code


def test_ocr_pdf_maps_timeout_to_normalized_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _make_client(handler)

    with pytest.raises(DocPipeError) as exc_info:
        client.ocr_pdf(b"%PDF-1.4 ...", "document.pdf", ["deu"])

    assert exc_info.value.code == "timeout"


def test_ocr_pdf_passes_its_own_timeout_override_extract_text_does_not():
    """OCR can take far longer than plain text extraction, so ocr_pdf must
    pass its own (longer) per-request timeout override to httpx, while
    extract_text must pass none at all - an explicit `timeout=None` to
    httpx means "no timeout", not "use the client default", so omitting
    the kwarg entirely for extract_text is the correct behavior, not an
    oversight (see the comment in stirling.py's _post)."""
    from dataclasses import replace

    settings = StirlingSettings(base_url="http://stirling.test", api_key="secret-key", timeout_seconds=90)
    settings = replace(settings, ocr=replace(settings.ocr, timeout_seconds=180))
    client = StirlingClient(settings)
    client._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b"ok")),
        follow_redirects=False,
    )

    captured_kwargs = []
    real_post = client._client.post

    def spy_post(*args, **kwargs):
        captured_kwargs.append(kwargs)
        return real_post(*args, **kwargs)

    client._client.post = spy_post

    client.extract_text(b"%PDF-1.4 ...", "document.pdf")
    client.ocr_pdf(b"%PDF-1.4 ...", "document.pdf", ["deu"])

    assert "timeout" not in captured_kwargs[0]
    assert captured_kwargs[1]["timeout"] == 180
