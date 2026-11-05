"""Thin client around the Stirling-PDF HTTP API.

This is the ONLY place in DocPipe that knows anything about Stirling
(its base URL, auth header, endpoint paths, and response shapes). Route
handlers never talk to Stirling directly and never see raw Stirling
errors or payloads.

Text extraction - verified against the current Stirling-PDF source
(Stirling-Tools/Stirling-PDF, `ConvertPDFToOffice` controller under
`/api/v1/convert`):

  POST {base_url}/api/v1/convert/pdf/text
  Header: X-API-KEY: <stirling api key>
  Multipart field: fileInput=<the pdf>
  Form field: outputFormat=txt
  Response: 200 with Content-Type text/plain, body = extracted text.

OCR (scanned-PDF fallback) - verified against the Stirling-PDF 2.14.2
source tag (`OCRController` + `ProcessPdfWithOcrRequest` under
`/api/v1/misc`):

  POST {base_url}/api/v1/misc/ocr-pdf
  Header: X-API-KEY: <stirling api key>
  Multipart field: fileInput=<the pdf>
  Form fields: languages=<repeated, one per language code, e.g. "deu"/"eng">,
               ocrType=skip-text, ocrRenderType=hocr
  Response: 200 with Content-Type application/pdf, body = the OCR'd PDF
  (this is the synchronous default; no `async=true` is sent, same as the
  text-extraction call above).

`ocrType=skip-text` only OCRs pages that don't already have extractable
text - the right choice here since OCR only ever runs as a fallback after
a first extraction attempt, on a document DocPipe doesn't otherwise know
the per-page structure of. `sidecar` is left at its default `false` so
Stirling returns a single PDF (not a PDF+text zip) - the resulting PDF is
fed straight back into `extract_text`, not parsed here, so DocPipe has
exactly one text-parsing code path.

Conversion to PDF (Contract Import V2.1, `POST /api/v1/documents/convert-to-pdf`)
- verified against the Stirling-PDF 2.14.2 source tag (`@ConvertApi` =
`/api/v1/convert`):

  Office/text (`ConvertOfficeController`, LibreOffice):
  POST {base_url}/api/v1/convert/file/pdf
  Multipart field: fileInput=<the file>
  Stirling decides the input type by the FILE NAME EXTENSION (validated
  against ^[a-zA-Z0-9]{2,4}$) and strips external references from OOXML/ODF
  documents before LibreOffice sees them (`OfficeDocumentSanitizer`), so
  DocPipe always sends a server-chosen name "document.<ext>" - never the
  client's file name.
  Response: 200 application/pdf.

  Images (`ConvertImgPDFController.convertToPdf`, `PdfUtils.imageToPdf`):
  POST {base_url}/api/v1/convert/img/pdf
  Multipart field: fileInput=<the image> (the endpoint accepts several; we
  send exactly one - one image is one document, no page grouping)
  Form fields: fitOption=maintainAspectRatio, colorType=color, autoRotate=false
  TIFF is recognised by the ".tif"/".tiff" file name (all pages of a
  multi-page TIFF become pages), JPEG by its "image/jpeg" content type.
  Response: 200 application/pdf.

A conversion answered with HTTP 500 means Stirling is up but could not
convert THIS file (LibreOffice/ImageIO failure on a broken or exotic
document) - reported as `conversion_failed`, not as an outage, so a caller
does not pause a whole batch for one broken file. 502/503/504 (a gateway in
front of Stirling) and connection errors stay `upstream_unavailable`.
"""

from __future__ import annotations

from collections.abc import Sequence

import httpx

from system.config import StirlingSettings
from system.errors import DocPipeError

EXTRACT_TEXT_PATH = "/api/v1/convert/pdf/text"
OCR_PDF_PATH = "/api/v1/misc/ocr-pdf"
OFFICE_TO_PDF_PATH = "/api/v1/convert/file/pdf"
IMAGE_TO_PDF_PATH = "/api/v1/convert/img/pdf"

# source format -> (Stirling path, content type sent with the file)
CONVERSION_TARGETS: dict[str, tuple[str, str]] = {
    "docx": (OFFICE_TO_PDF_PATH, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "doc": (OFFICE_TO_PDF_PATH, "application/msword"),
    "odt": (OFFICE_TO_PDF_PATH, "application/vnd.oasis.opendocument.text"),
    "rtf": (OFFICE_TO_PDF_PATH, "application/rtf"),
    "txt": (OFFICE_TO_PDF_PATH, "text/plain"),
    "jpg": (IMAGE_TO_PDF_PATH, "image/jpeg"),
    "jpeg": (IMAGE_TO_PDF_PATH, "image/jpeg"),
    "png": (IMAGE_TO_PDF_PATH, "image/png"),
    "tif": (IMAGE_TO_PDF_PATH, "image/tiff"),
    "tiff": (IMAGE_TO_PDF_PATH, "image/tiff"),
}


class StirlingClient:
    def __init__(self, settings: StirlingSettings) -> None:
        self._settings = settings
        # Redirects are not followed: the upstream URL is server-configured,
        # never client-controlled, and a redirect could otherwise be used to
        # smuggle the request (and its X-API-KEY) to an unintended host.
        self._client = httpx.Client(follow_redirects=False, timeout=settings.timeout_seconds)

    def close(self) -> None:
        self._client.close()

    def _post(
        self,
        path: str,
        file_bytes: bytes,
        filename: str,
        data: dict,
        timeout_seconds: float | None = None,
        content_type: str = "application/pdf",
        server_error_code: str = "upstream_unavailable",
    ) -> httpx.Response:
        url = self._settings.base_url.rstrip("/") + path
        headers = {"X-API-KEY": self._settings.api_key}
        files = {"fileInput": (filename, file_bytes, content_type)}
        # httpx treats an explicit timeout=None as "no timeout at all", not
        # "use the client's default" - only override per-request when a
        # real value was given (the OCR call's own, longer timeout).
        extra_kwargs = {} if timeout_seconds is None else {"timeout": timeout_seconds}

        try:
            response = self._client.post(url, headers=headers, files=files, data=data, **extra_kwargs)
        except httpx.TimeoutException as exc:
            raise DocPipeError("timeout", "Document processing timed out.") from exc
        except httpx.HTTPError as exc:
            raise DocPipeError(
                "upstream_unavailable", "Document processing service is temporarily unavailable."
            ) from exc

        if response.status_code in (401, 403):
            raise DocPipeError(
                "upstream_auth_failed", "Document processing service rejected the request."
            )
        if response.status_code in (400, 422, 500) and server_error_code != "upstream_unavailable":
            raise DocPipeError(server_error_code, "Document could not be converted.")
        if response.status_code >= 500:
            raise DocPipeError(
                "upstream_unavailable", "Document processing service is temporarily unavailable."
            )
        if response.status_code != 200:
            raise DocPipeError("processing_failed", "Document processing failed.")

        return response

    def extract_text(self, file_bytes: bytes, filename: str) -> str:
        response = self._post(EXTRACT_TEXT_PATH, file_bytes, filename, data={"outputFormat": "txt"})
        return response.text

    def ocr_pdf(self, file_bytes: bytes, filename: str, languages: Sequence[str]) -> bytes:
        # httpx's multipart encoder yields one field per list item for a
        # given key - this is how a repeated `languages=deu`/`languages=eng`
        # multipart field set is produced, not a single "deu+eng" value.
        data = {
            "languages": list(languages),
            "ocrType": "skip-text",
            "ocrRenderType": "hocr",
        }
        response = self._post(
            OCR_PDF_PATH,
            file_bytes,
            filename,
            data=data,
            timeout_seconds=self._settings.ocr.timeout_seconds,
        )
        return response.content

    def convert_to_pdf(self, file_bytes: bytes, source_format: str) -> bytes:
        """Convert one Office/text/image document to PDF bytes.

        `source_format` must be a key of CONVERSION_TARGETS (validated by
        the route); the upload name is always "document.<format>". The
        returned bytes are NOT trusted here - the route validates them as a
        PDF before answering.
        """
        path, content_type = CONVERSION_TARGETS[source_format]
        data = {}
        if path == IMAGE_TO_PDF_PATH:
            data = {"fitOption": "maintainAspectRatio", "colorType": "color", "autoRotate": "false"}
        response = self._post(
            path,
            file_bytes,
            "document." + source_format,
            data=data,
            content_type=content_type,
            server_error_code="conversion_failed",
        )
        return response.content
