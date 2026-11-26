"""PDF4Fill form endpoints: inspect (Phase 2.1) and fill (Phase 2.2).

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
import re
import time

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from system.auth import get_authenticated_client, require_forms_service
from system.config import ClientConfig, Settings
from system.errors import DocPipeError
from system.forms import fill as fill_mod
from system.forms.inspect import inspect_pdf
from system.forms.schemas import FILL_RESPONSES, INSPECT_RESPONSES, FillManifest
from system.monitoring.context import note_pages
from system.monitoring.context import step as monitoring_step
from system.routes.documents import _assert_is_pdf, _spool_upload_to_temp_file

router = APIRouter()
logger = logging.getLogger("docpipe")

INSPECT_PATH = "/api/v1/documents/forms/inspect"
_MODES = ("auto", "off")
_IMAGE_REF = re.compile(r"[A-Za-z0-9_.:\-]{1,64}")


@router.post(
    INSPECT_PATH,
    responses=INSPECT_RESPONSES,
    summary="Inspect a PDF form template (PDF4Fill, services.forms)",
    description="Native fields, layout proposals, page text with positions, OCR for pages without a "
    "text layer, SHA-256 and security facts. See docs/forms-inspect.md.",
)
def forms_inspect(
    request: Request,
    file: UploadFile = File(...),
    password: str | None = Form(
        default=None, description="Open or owner password; body only, never logged or returned."),
    ocr: str = Form(
        default="auto", json_schema_extra={"enum": ["auto", "off"]},
        description="auto: OCR pages without a text layer via Stirling (if enabled on the server); "
                    "off: never."),
    detect: str = Form(
        default="auto", json_schema_extra={"enum": ["auto", "off"]},
        description="auto: add layout proposals for static fields (source: detected); "
                    "off: native fields only."),
    client: ClientConfig = Depends(get_authenticated_client),
) -> JSONResponse:
    require_forms_service(client)
    settings: Settings = request.app.state.settings
    stirling = request.app.state.stirling_client
    ocr_mode, detect_mode = ocr.strip().lower(), detect.strip().lower()
    if ocr_mode not in _MODES or detect_mode not in _MODES:
        raise DocPipeError("invalid_request", "ocr and detect must be 'auto' or 'off'.")

    ocr_settings = settings.stirling.ocr
    runner = None
    if ocr_mode == "auto" and ocr_settings.enabled:
        def runner(pdf_bytes: bytes) -> bytes:
            return stirling.ocr_pdf(pdf_bytes, "document.pdf", ocr_settings.languages)

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
        # Monitoring (no-op when disabled): duration of the in-process
        # analysis (includes any Stirling OCR call, which is also recorded
        # as its own step) and the page count inspect already determined.
        with monitoring_step("forms.inspect"):
            data = inspect_pdf(
                file_bytes,
                password or None,
                max_pages=settings.forms.max_pages,
                max_fields=settings.forms.max_fields,
                ocr=runner,
                ocr_skipped="off" if ocr_mode == "off" else "disabled",
                max_ocr_pages=settings.forms.max_ocr_pages,
                detect=detect_mode == "auto",
            )
        note_pages(data.get("page_count"))
    except DocPipeError as exc:
        status = exc.code
        raise
    finally:
        # Metadata only - never file names, field names, text or the password.
        security = data.get("security") or {}
        fields = data.get("fields") or []
        logger.info(
            "forms_inspect request_id=%s client_id=%s input_bytes=%s pages=%s fields=%s detected=%s "
            "ocr=%s ocr_pages=%s encrypted=%s xfa=%s signed=%s warnings=%s status=%s duration_ms=%s",
            getattr(request.state, "request_id", None),
            client.client_id,
            input_bytes,
            data.get("page_count"),
            sum(1 for f in fields if f.get("source") == "native"),
            sum(1 for f in fields if f.get("source") == "detected"),
            (data.get("ocr") or {}).get("status"),
            len((data.get("ocr") or {}).get("pages") or []),
            security.get("encrypted"),
            security.get("xfa"),
            security.get("signed"),
            len(data.get("warnings") or []),
            status,
            round((time.monotonic() - started_at) * 1000, 1),
        )

    return JSONResponse(content={"ok": True, "data": data}, headers={"Cache-Control": "no-store"})


FILL_PATH = "/api/v1/documents/forms/fill"


def _read_upload(upload: UploadFile, max_bytes: int) -> bytes:
    temp_path = _spool_upload_to_temp_file(upload, max_bytes)
    try:
        with open(temp_path, "rb") as fh:
            return fh.read()
    finally:
        os.remove(temp_path)


@router.post(
    FILL_PATH,
    responses=FILL_RESPONSES,
    response_class=Response,
    summary="Fill a PDF form template and return a flattened PDF (PDF4Fill, services.forms)",
    description="Native fields by native_ref, overlay fields and PNG images by page + position (inspect "
    "coordinates), bundled Unicode fonts, auto-fit, no truncation. The manifest is a JSON string; its "
    "schema is in x-docpipe-manifest-schema. See docs/forms-fill.md.",
    openapi_extra={"x-docpipe-manifest-schema": FillManifest.model_json_schema()},
)
def forms_fill(
    request: Request,
    file: UploadFile = File(..., description="The unchanged PDF template (sha256 from inspect)."),
    manifest: str = Form(..., description="JSON document, schema x-docpipe-manifest-schema."),
    password: str | None = Form(
        default=None, description="Open or owner password; body only, never logged or returned."),
    images: list[UploadFile] = File(
        default=[], description="PNG parts; each part's filename is the image_ref used in the manifest."),
    client: ClientConfig = Depends(get_authenticated_client),
) -> Response:
    require_forms_service(client)
    settings: Settings = request.app.state.settings

    started_at = time.monotonic()
    status = "success"
    result = None
    input_bytes = 0
    preview = None
    try:
        parsed = fill_mod.parse_manifest(manifest)
        preview = parsed.output.preview
        file_bytes = _read_upload(file, settings.server.max_file_size_bytes)
        input_bytes = len(file_bytes)
        if not file_bytes.startswith(b"%PDF-"):
            raise DocPipeError("invalid_file", "Uploaded file is not a valid PDF.")
        if len(images) > fill_mod.MAX_IMAGES:
            raise DocPipeError("limit_exceeded", "Too many images.")
        parts: dict[str, bytes] = {}
        for upload in images:
            ref = upload.filename or ""
            if not _IMAGE_REF.fullmatch(ref) or ref in parts:
                raise DocPipeError("invalid_image", "Each image part needs a unique, simple file name.",
                                   details={"reason": "image_ref"})
            parts[ref] = _read_upload(upload, fill_mod.MAX_IMAGE_BYTES)
        images_loaded = fill_mod.load_images(parts)
        with monitoring_step("forms.fill"):
            result = fill_mod.fill_pdf(
                file_bytes,
                parsed,
                images_loaded,
                password or None,
                max_pages=settings.forms.max_pages,
                max_fields=settings.forms.max_fields,
            )
        note_pages(result.pages)
    except DocPipeError as exc:
        status = exc.code
        raise
    finally:
        # Metadata only - never field ids, values, file names, images or the password.
        logger.info(
            "forms_fill request_id=%s client_id=%s input_bytes=%s output_bytes=%s pages=%s native=%s "
            "overlay=%s images=%s preview=%s encrypted=%s status=%s duration_ms=%s",
            getattr(request.state, "request_id", None),
            client.client_id,
            input_bytes,
            len(result.pdf) if result else 0,
            result.pages if result else None,
            result.native_fields if result else None,
            result.overlay_fields if result else None,
            result.images if result else None,
            preview,
            result.encrypted if result else None,
            status,
            round((time.monotonic() - started_at) * 1000, 1),
        )

    return Response(content=result.pdf, media_type="application/pdf",
                    headers={"Cache-Control": "no-store",
                             "X-DocPipe-Preview": "true" if preview else "false"})
