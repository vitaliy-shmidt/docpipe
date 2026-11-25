"""OpenAPI description of POST /api/v1/documents/forms/inspect (schema_version "1").

Documentation models only: the route returns the dict built by
system/forms/inspect.py unchanged. tests/test_forms_openapi.py validates real
responses against the JSON schema generated from these models, and keeps the
committed snapshot docs/openapi/forms.openapi.json in sync.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Position(_Strict):
    """Visible page: points, origin top-left, page rotation applied, inside the page."""

    x: float
    y: float
    width: float
    height: float


class TextBlock(_Strict):
    text: str
    position: Position


class Page(_Strict):
    page: int = Field(ge=1)
    width: float
    height: float
    rotation: Literal[0, 90, 180, 270]
    text_source: Literal["embedded", "ocr", "none"]
    text: str
    text_blocks: list[TextBlock]


class Option(_Strict):
    value: str
    label: str


class Label(_Strict):
    text: str
    position: Position


class FormField(_Strict):
    """A native widget (`source: native`) or a layout proposal (`source: detected`)."""

    id: str = Field(description="field_NNN (native, reading order) or detected_NNN (proposal)")
    name: str | None = Field(description="Display name: /TU or input_name (native); label text or null")
    input_name: str | None = Field(description="Fully qualified technical name; null for proposals")
    native_ref: str | None
    field_ref: str | None
    page: int = Field(ge=1)
    position: Position
    source: Literal["native", "detected"]
    type: Literal["text", "checkbox", "radio", "choice", "signature"]
    options: list[Option]
    # native only
    widget_index: int | None = None
    widget_count: int | None = None
    required: bool | None = None
    read_only: bool | None = None
    hidden: bool | None = None
    multiline: bool | None = None
    max_length: int | None = None
    password_field: bool | None = None
    export_value: str | None = None
    choice_style: Literal["combo", "list"] | None = None
    editable: bool | None = None
    multi_select: bool | None = None
    signed: bool | None = None
    # detected only
    detection: Literal["underline", "box", "checkbox", "placeholder"] | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    label: Label | None = None


class OcrPage(_Strict):
    page: int = Field(ge=1)
    geometry: Literal["identical", "rescaled", "mismatch"]
    text_found: bool


class Ocr(_Strict):
    status: Literal["not_needed", "applied", "partial", "failed", "disabled", "off", "not_permitted"]
    pages: list[OcrPage]
    reason: str | None = None


class Detection(_Strict):
    enabled: bool
    count: int = Field(ge=0)


class Permissions(_Strict):
    extract_text: bool
    fill_forms: bool
    modify_annotations: bool
    modify: bool
    assemble: bool
    print: bool


class Security(_Strict):
    encrypted: bool
    open_password_required: bool
    encryption_method: str | None
    permissions: Permissions | None
    signed: bool
    certified: bool
    xfa: bool


class InspectWarning(BaseModel):
    """`code` is the stable key; optional `page`, `count`, `field_ref`, `reason`, `low_confidence_count`."""

    model_config = ConfigDict(extra="allow")

    code: str
    message: str


class InspectData(_Strict):
    schema_version: Literal["1"]
    sha256: str = Field(pattern="^[0-9a-f]{64}$")
    page_count: int = Field(ge=1)
    text: str
    pages: list[Page]
    fields: list[FormField]
    ocr: Ocr
    detection: Detection
    security: Security
    warnings: list[InspectWarning]


class InspectResponse(_Strict):
    ok: Literal[True]
    data: InspectData


class ErrorResponse(_Strict):
    ok: Literal[False]
    code: str
    message: str
    details: dict | None = Field(default=None, description="forms endpoints only, e.g. field_id, "
                                 "reason, permission, path - never submitted values")


INSPECT_RESPONSES = {
    200: {"model": InspectResponse, "description": "Inspection result (`Cache-Control: no-store`)."},
    400: {"model": ErrorResponse, "description": "`invalid_file`, `invalid_request`"},
    401: {"model": ErrorResponse, "description": "`unauthorized`"},
    403: {"model": ErrorResponse, "description": "`forms_disabled`"},
    413: {"model": ErrorResponse, "description": "`file_too_large`, `limit_exceeded`"},
    422: {"model": ErrorResponse,
          "description": "`password_required`, `invalid_password`, `xfa_unsupported`"},
}


# --- forms/fill (Phase 2.2) ------------------------------------------------------------------------

_ID_PATTERN = r"^[A-Za-z0-9_.:\-]{1,64}$"


class FillStyle(_Strict):
    """Fully resolved effective style of a text value (PDF4Fill resolves inheritance)."""

    font_family: Literal["sans", "serif", "mono"] = "sans"
    font_size: float = Field(default=10.0, ge=4, le=72, description="Points")
    color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$", description="Hex RGB")
    font_weight: Literal["normal", "bold"] = "normal"
    font_style: Literal["normal", "italic"] = "normal"
    align: Literal["left", "center", "right"] = "left"
    auto_fit: bool = Field(default=False, description="Shrink in 0.5 pt steps down to min_font_size")
    min_font_size: float = Field(default=6.0, ge=4, le=72)
    multiline: bool | None = Field(default=None, description="Override; default: native flag, else false")


class FillPosition(_Strict):
    """Visible page, points, origin top-left (the inspect coordinate system)."""

    x: float = Field(ge=0)
    y: float = Field(ge=0)
    width: float = Field(gt=0)
    height: float = Field(gt=0)


class FillField(_Strict):
    id: str = Field(pattern=_ID_PATTERN, description="Caller's id; echoed in error details only")
    mode: Literal["native", "overlay"]
    type: Literal["text", "number", "date", "choice", "checkbox", "radio", "stamp_image", "signature_image"]
    native_ref: str | None = Field(default=None, max_length=64, description="mode=native: from inspect")
    input_name: str | None = Field(default=None, max_length=1024, description="mode=native: optional check")
    page: int | None = Field(default=None, ge=1, description="mode=overlay: required; native: optional check")
    position: FillPosition | None = Field(default=None, description="mode=overlay: required; native: check")
    value: str | bool | list[str] | None = Field(
        default=None, description="text/number/date/choice: string (pre-formatted by the caller); "
                                  "choice with multi_select: list; checkbox: bool or export value; "
                                  "radio: export value; images: unused")
    required: bool = False
    image_ref: str | None = Field(default=None, pattern=_ID_PATTERN,
                                  description="Images: filename of one `images` part")
    style: FillStyle = Field(default_factory=FillStyle)


class FillOutput(_Strict):
    flatten: Literal[True] = Field(
        default=True, description="Only flattened, print-ready output is supported")
    preview: bool = Field(default=False, description="Same rendering plus a PREVIEW watermark on every page")


class FillManifest(_Strict):
    schema_version: Literal["1"]
    sha256: str = Field(
        pattern="^[0-9a-f]{64}$", description="SHA-256 of the uploaded template (from inspect)")
    fields: list[FillField]
    output: FillOutput = Field(default_factory=FillOutput)


FILL_RESPONSES = {
    200: {"content": {"application/pdf": {"schema": {"type": "string", "format": "binary"}}},
          "description": "The filled, flattened PDF (`Cache-Control: no-store`). An encrypted template "
                         "yields an output with the same encryption and permissions."},
    400: {"model": ErrorResponse, "description": "`invalid_file`, `invalid_request`, `invalid_manifest`, "
                                                 "`invalid_image`"},
    401: {"model": ErrorResponse, "description": "`unauthorized`"},
    403: {"model": ErrorResponse, "description": "`forms_disabled`"},
    409: {"model": ErrorResponse, "description": "`hash_mismatch`"},
    413: {"model": ErrorResponse, "description": "`file_too_large`, `limit_exceeded`"},
    422: {"model": ErrorResponse,
          "description": "`password_required`, `invalid_password`, `xfa_unsupported`, `permission_denied`, "
                         "`signature_conflict`, `unknown_field`, `field_conflict`, `invalid_geometry`, "
                         "`invalid_value`, `text_overflow`, `unsupported_characters`"},
}
