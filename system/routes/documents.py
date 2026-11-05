"""Authenticated document processing endpoints.

V1 exposes a single operation: extracting text from a PDF via Stirling.
Uploaded files are never persisted; they are written to a uniquely named
temp file only for the duration of the request and removed afterwards,
on both the success and the error path.

V1.1 adds an OCR fallback for scanned/image-only PDFs, entirely internal
to this one operation - the API contract is unchanged except for the
additive `extraction_method` field:

    plain text extraction
          |
          v
    enough real text? --yes--> done (extraction_method="embedded_text")
          |
          no
          v
    OCR the original PDF (Stirling) -> plain text extraction again
          |
          v
    done (extraction_method="ocr")

OCR only ever runs after a *technically successful* first extraction that
came back with too little text (see system/text_quality.py). A technical
failure of the first extraction (timeout, upstream down, auth failure,
...) never triggers OCR - that error is returned as-is. Exactly one OCR
attempt is made; there is no retry loop and no second OCR pass.

V2.1 (HubDix Contract Import ZIP) adds `/documents/convert-to-pdf`: one
Office/text/image upload -> Stirling -> PDF bytes, re-validated before it
is returned. Same temp-file handling and size limit as extract-text.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import Response

from system.auth import get_authenticated_client, require_documents_service
from system.config import ClientConfig, Settings
from system.errors import DocPipeError
from system.schemas import (
    EXTRACTION_METHOD_EMBEDDED_TEXT,
    EXTRACTION_METHOD_OCR,
    ExtractTextData,
    ExtractTextResponse,
)
from system.services.stirling import CONVERSION_TARGETS
from system.text_quality import is_text_sufficient

router = APIRouter()
logger = logging.getLogger("docpipe")

_CHUNK_SIZE = 1024 * 1024
_PDF_MAGIC = b"%PDF-"


def _spool_upload_to_temp_file(upload: UploadFile, max_size_bytes: int) -> str:
    """Write the upload to a uniquely named temp file, enforcing the size limit.

    The client-supplied filename is never used to build the storage path.
    """
    fd, path = tempfile.mkstemp(prefix="docpipe_", suffix=".upload")
    total = 0
    try:
        with os.fdopen(fd, "wb") as tmp:
            while True:
                chunk = upload.file.read(_CHUNK_SIZE)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_size_bytes:
                    raise DocPipeError("file_too_large", "Uploaded file exceeds the maximum allowed size.")
                tmp.write(chunk)
    except Exception:
        os.remove(path)
        raise

    if total == 0:
        os.remove(path)
        raise DocPipeError("invalid_file", "Uploaded file is empty.")

    return path


def _assert_is_pdf(path: str) -> None:
    with open(path, "rb") as fh:
        header = fh.read(len(_PDF_MAGIC))
    if header != _PDF_MAGIC:
        raise DocPipeError("invalid_file", "Uploaded file is not a valid PDF.")


@router.post("/api/v1/documents/extract-text", response_model=ExtractTextResponse)
def extract_text(
    request: Request,
    file: UploadFile = File(...),
    client: ClientConfig = Depends(get_authenticated_client),
) -> ExtractTextResponse:
    require_documents_service(client)

    settings: Settings = request.app.state.settings
    stirling = request.app.state.stirling_client

    started_at = time.monotonic()
    temp_path = _spool_upload_to_temp_file(file, settings.server.max_file_size_bytes)
    try:
        _assert_is_pdf(temp_path)
        with open(temp_path, "rb") as fh:
            file_bytes = fh.read()

        text = stirling.extract_text(file_bytes, "document.pdf")
        initial_text_length = len(text)
        extraction_method = EXTRACTION_METHOD_EMBEDDED_TEXT

        ocr = settings.stirling.ocr
        ocr_attempted = ocr.enabled and not is_text_sufficient(text, ocr.min_meaningful_characters)
        if ocr_attempted:
            ocr_pdf_bytes = stirling.ocr_pdf(file_bytes, "document.pdf", ocr.languages)
            text = stirling.extract_text(ocr_pdf_bytes, "document.pdf")
            extraction_method = EXTRACTION_METHOD_OCR
    finally:
        os.remove(temp_path)

    logger.info(
        "extract_text request_id=%s client_id=%s input_bytes=%s initial_text_length=%s "
        "ocr_attempted=%s extraction_method=%s final_text_length=%s duration_ms=%s",
        getattr(request.state, "request_id", None),
        client.client_id,
        len(file_bytes),
        initial_text_length,
        ocr_attempted,
        extraction_method,
        len(text),
        round((time.monotonic() - started_at) * 1000, 1),
    )

    return ExtractTextResponse(
        ok=True, data=ExtractTextData(text=text, text_length=len(text), extraction_method=extraction_method)
    )


# Contract Import V2.1: technical conversion of one supported document to
# PDF. No business logic - DocPipe does not know hotels, drafts or what the
# document is for; the caller (HubDix) re-validates the PDF and decides.
# The format is named by the caller (it validated the file first) and is
# checked again here against the file's own magic bytes - an upload is
# never forwarded to Stirling on the strength of a name alone.
_FORMAT_MAGIC: dict[str, tuple[bytes, ...]] = {
    "docx": (b"PK\x03\x04",),
    "odt": (b"PK\x03\x04",),
    "doc": (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1",),
    "rtf": (b"{\\rtf",),
    "jpg": (b"\xff\xd8\xff",),
    "jpeg": (b"\xff\xd8\xff",),
    "png": (b"\x89PNG\r\n\x1a\n",),
    "tif": (b"II*\x00", b"MM\x00*"),
    "tiff": (b"II*\x00", b"MM\x00*"),
}
_UTF8_BOM = b"\xef\xbb\xbf"


def _assert_format(source_format: str, file_bytes: bytes) -> None:
    if source_format == "txt":
        # Plain text has no magic: reject binary content (NUL bytes).
        if b"\x00" in file_bytes:
            raise DocPipeError("invalid_file", "Uploaded file is not a plain text document.")
        return
    if not file_bytes.startswith(_FORMAT_MAGIC[source_format]):
        raise DocPipeError("invalid_file", "Uploaded file does not match the declared format.")


def _normalize_text(file_bytes: bytes) -> bytes:
    """LibreOffice guesses the encoding of a .txt file - a UTF-8 BOM makes
    it unambiguous. Non-UTF-8 text is read as Windows-1252 (the usual
    legacy encoding of such files) and re-encoded."""
    if file_bytes.startswith(_UTF8_BOM):
        return file_bytes
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError:
        text = file_bytes.decode("cp1252", errors="replace")
    return _UTF8_BOM + text.encode("utf-8")


@router.post("/api/v1/documents/convert-to-pdf")
def convert_to_pdf(
    request: Request,
    file: UploadFile = File(...),
    source_format: str = Form(...),
    client: ClientConfig = Depends(get_authenticated_client),
) -> Response:
    """Convert one Office/text/image document to PDF (binary response).

    Success: 200, `Content-Type: application/pdf`, body = PDF bytes,
    header `X-DocPipe-Source-Format`. Errors: the normal JSON error body
    (`unsupported_format`, `invalid_file`, `file_too_large`,
    `conversion_failed`, `upstream_unavailable`, `timeout`, ...). The upload
    is held in a temp file for the request only and removed afterwards.
    """
    require_documents_service(client)

    settings: Settings = request.app.state.settings
    stirling = request.app.state.stirling_client
    fmt = source_format.strip().lower()
    if fmt not in CONVERSION_TARGETS:
        raise DocPipeError("unsupported_format", "This document format cannot be converted to PDF.")

    started_at = time.monotonic()
    temp_path = _spool_upload_to_temp_file(file, settings.server.max_file_size_bytes)
    try:
        with open(temp_path, "rb") as fh:
            file_bytes = fh.read()
    finally:
        os.remove(temp_path)
    _assert_format(fmt, file_bytes)
    if fmt == "txt":
        file_bytes = _normalize_text(file_bytes)

    status = "success"
    pdf_bytes = b""
    try:
        pdf_bytes = stirling.convert_to_pdf(file_bytes, fmt)
        # The converter's output is a new, untrusted input: it must be a
        # non-empty PDF within the size limit before it is handed out.
        if not pdf_bytes.startswith(_PDF_MAGIC):
            raise DocPipeError("conversion_failed", "Document could not be converted.")
        if len(pdf_bytes) > settings.server.max_file_size_bytes:
            raise DocPipeError("file_too_large", "Converted document exceeds the maximum allowed size.")
    except DocPipeError as exc:
        status = exc.code
        raise
    finally:
        # Metadata only - never the file name or any content.
        logger.info(
            "convert_to_pdf request_id=%s client_id=%s source_format=%s input_bytes=%s "
            "output_bytes=%s status=%s duration_ms=%s",
            getattr(request.state, "request_id", None),
            client.client_id,
            fmt,
            len(file_bytes),
            len(pdf_bytes),
            status,
            round((time.monotonic() - started_at) * 1000, 1),
        )

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"X-DocPipe-Source-Format": fmt, "Cache-Control": "no-store"},
    )
