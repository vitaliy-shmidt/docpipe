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
"""

from __future__ import annotations

import logging
import os
import tempfile
import time

from fastapi import APIRouter, Depends, File, Request, UploadFile

from system.auth import get_authenticated_client, require_documents_service
from system.config import ClientConfig, Settings
from system.errors import DocPipeError
from system.schemas import (
    EXTRACTION_METHOD_EMBEDDED_TEXT,
    EXTRACTION_METHOD_OCR,
    ExtractTextData,
    ExtractTextResponse,
)
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
