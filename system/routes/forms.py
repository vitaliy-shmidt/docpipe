"""PDF4Fill form endpoints (Phase 2.1a: inspect only).

`POST /api/v1/documents/forms/inspect` - multipart `file` (PDF), optional
`password` (body only; never logged, never echoed). Requires the separate
`services.forms` flag. Same temp-file handling and size limit as
extract-text; the upload is removed on every path.

Response: `{"ok": true, "data": {...}}` with the structure documented in
README "POST /api/v1/documents/forms/inspect" and produced by
system/forms/inspect.py. Errors use the normal `{ok:false, code, message}`
body.
"""

from __future__ import annotations

import logging
import os
import time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

from system.auth import get_authenticated_client, require_forms_service
from system.config import ClientConfig, Settings
from system.errors import DocPipeError
from system.forms.inspect import inspect_pdf
from system.routes.documents import _assert_is_pdf, _spool_upload_to_temp_file

router = APIRouter()
logger = logging.getLogger("docpipe")

INSPECT_PATH = "/api/v1/documents/forms/inspect"


@router.post(INSPECT_PATH)
def forms_inspect(
    request: Request,
    file: UploadFile = File(...),
    password: str | None = Form(default=None),
    client: ClientConfig = Depends(get_authenticated_client),
) -> JSONResponse:
    require_forms_service(client)
    settings: Settings = request.app.state.settings

    started_at = time.monotonic()
    status = "success"
    data: dict = {}
    input_bytes = 0
    try:
        temp_path = _spool_upload_to_temp_file(file, settings.server.max_file_size_bytes)
        try:
            _assert_is_pdf(temp_path)
            with open(temp_path, "rb") as fh:
                file_bytes = fh.read()
        finally:
            os.remove(temp_path)
        input_bytes = len(file_bytes)
        data = inspect_pdf(
            file_bytes,
            password or None,
            max_pages=settings.forms.max_pages,
            max_fields=settings.forms.max_fields,
        )
    except DocPipeError as exc:
        status = exc.code
        raise
    finally:
        # Metadata only - never file names, field names, text or the password.
        security = data.get("security") or {}
        logger.info(
            "forms_inspect request_id=%s client_id=%s input_bytes=%s pages=%s fields=%s encrypted=%s "
            "xfa=%s signed=%s warnings=%s status=%s duration_ms=%s",
            getattr(request.state, "request_id", None),
            client.client_id,
            input_bytes,
            data.get("page_count"),
            len(data.get("fields") or []),
            security.get("encrypted"),
            security.get("xfa"),
            security.get("signed"),
            len(data.get("warnings") or []),
            status,
            round((time.monotonic() - started_at) * 1000, 1),
        )

    return JSONResponse(content={"ok": True, "data": data}, headers={"Cache-Control": "no-store"})
