"""Normalized error codes shared by all DocPipe API responses."""

from __future__ import annotations

# error code -> default HTTP status code
STATUS_BY_CODE: dict[str, int] = {
    "unauthorized": 401,
    "service_disabled": 403,
    "invalid_file": 400,
    # Malformed JSON request body (missing/wrong-typed field) on a non-file
    # endpoint (e.g. /documents/analyze) - kept distinct from invalid_file,
    # which is specifically about an uploaded document.
    "invalid_request": 400,
    "file_too_large": 413,
    "upstream_unavailable": 502,
    "upstream_auth_failed": 502,
    "processing_failed": 502,
    "timeout": 504,
    "internal_error": 500,
    # AI analysis (V2)
    "ai_disabled": 403,
    "unknown_mode": 400,
    "input_too_large": 413,
    "ai_unavailable": 502,
    "ai_timeout": 504,
    "ai_invalid_response": 502,
    "ai_processing_failed": 500,
    # Config/runtime inconsistency: the mode's (or the client override's)
    # resolved model profile name has no matching entry under `models:`.
    # Should only ever happen if the running config drifted from what
    # config.py's startup validation checked - never a normal client error.
    "model_profile_unavailable": 502,
    # Prompt Lab (V2.2)
    "prompt_lab_disabled": 403,
    "unknown_prompt_version": 404,
    "invalid_prompt_version": 400,
    "prompt_version_exists": 409,
    "invalid_prompt": 400,
    "prompt_too_large": 413,
    # Assistant routing foundation (V2.2)
    "assistant_disabled": 403,
    # Conversion to PDF (Contract Import V2.1): a format outside the
    # supported list, and a supported, well-formed upload the converter
    # still could not turn into a PDF (broken/exotic document).
    "unsupported_format": 415,
    "conversion_failed": 422,
    # Forms inspect (Phase 2.1a, PDF4Fill). Messages never contain names,
    # values, text or the password.
    "forms_disabled": 403,
    "password_required": 422,
    "invalid_password": 422,
    "xfa_unsupported": 422,
    "limit_exceeded": 413,
    # Forms fill (Phase 2.2). `details` carries field ids/reasons, never values.
    "invalid_manifest": 400,
    "invalid_image": 400,
    "hash_mismatch": 409,
    "permission_denied": 422,
    "signature_conflict": 422,
    "unknown_field": 422,
    "field_conflict": 422,
    "invalid_geometry": 422,
    "invalid_value": 422,
    "text_overflow": 422,
    "unsupported_characters": 422,
}


class DocPipeError(Exception):
    """Raised anywhere in the app to produce a normalized error response.

    `status_code` may be omitted; it is then looked up from STATUS_BY_CODE.
    """

    def __init__(
        self, code: str, message: str, status_code: int | None = None, details: dict | None = None
    ) -> None:
        self.code = code
        self.message = message
        # Optional, additive: only the forms endpoints set it; every other error body is unchanged.
        self.details = details
        self.status_code = status_code or STATUS_BY_CODE.get(code, 500)
        super().__init__(message)
