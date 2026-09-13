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
}


class DocPipeError(Exception):
    """Raised anywhere in the app to produce a normalized error response.

    `status_code` may be omitted; it is then looked up from STATUS_BY_CODE.
    """

    def __init__(self, code: str, message: str, status_code: int | None = None) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code or STATUS_BY_CODE.get(code, 500)
        super().__init__(message)
