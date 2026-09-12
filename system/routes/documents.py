"""Authenticated document processing endpoints.

V1 exposes a single operation: extracting text from a PDF via Stirling.
Uploaded files are never persisted; they are written to a uniquely named
temp file only for the duration of the request and removed afterwards,
on both the success and the error path.
"""

from __future__ import annotations

import os
import tempfile

from fastapi import APIRouter, Depends, File, Request, UploadFile

from system.auth import get_authenticated_client, require_documents_service
from system.config import ClientConfig, Settings
from system.errors import DocPipeError
from system.schemas import ExtractTextData, ExtractTextResponse

router = APIRouter()

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
    temp_path = _spool_upload_to_temp_file(file, settings.server.max_file_size_bytes)
    try:
        _assert_is_pdf(temp_path)
        with open(temp_path, "rb") as fh:
            file_bytes = fh.read()
        text = request.app.state.stirling_client.extract_text(file_bytes, "document.pdf")
    finally:
        os.remove(temp_path)

    return ExtractTextResponse(ok=True, data=ExtractTextData(text=text, text_length=len(text)))
