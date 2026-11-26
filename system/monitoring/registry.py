"""Fixed registries for every monitoring dimension.

A value that is stored in the monitoring database is either a number, a UTC
timestamp, an id from the server config (client ids, model names) or one of
the enums below. Anything else is replaced by a fixed placeholder - never
stored as received. This is what makes log injection and accidental content
capture structurally impossible (no free-form string ever reaches storage).
"""

from __future__ import annotations

import re

from system.errors import STATUS_BY_CODE

UNMATCHED_ROUTE = "<unmatched>"
INVALID = "<invalid>"
UNREGISTERED = "<unregistered>"

# FastAPI route template -> service. A template missing here is still
# recorded (it is a registered route), with service "other".
SERVICE_BY_ROUTE: dict[str, str] = {
    "/api/v1/health": "system",
    "/api/v1/capabilities": "system",
    "/api/v1/documents/extract-text": "documents",
    "/api/v1/documents/convert-to-pdf": "documents",
    "/api/v1/documents/analyze": "ai",
    "/api/v1/documents/warmup": "ai",
    "/api/v1/documents/forms/inspect": "forms",
    "/api/v1/documents/forms/fill": "forms",
    "/api/v1/lab/prompts": "prompt_lab",
    "/api/v1/lab/prompts/{mode}": "prompt_lab",
    "/api/v1/lab/analyze": "prompt_lab",
    "/api/v1/lab/prompts/{mode}/versions": "prompt_lab",
    "/api/v1/lab/prompts/{mode}/activate": "prompt_lab",
    "/api/v1/assistant/query": "assistant",
    "/api/v1/assistant/warmup": "assistant",
}
SERVICES = frozenset({*SERVICE_BY_ROUTE.values(), "other", "none"})

# Routes whose successful response is one processed document.
DOCUMENT_ROUTES = frozenset(
    {
        "/api/v1/documents/extract-text",
        "/api/v1/documents/convert-to-pdf",
        "/api/v1/documents/forms/inspect",
        "/api/v1/documents/forms/fill",
    }
)

METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
OTHER_METHOD = "OTHER"

# Processing steps (request_steps.step). Steps may nest: forms.inspect
# includes any stirling.ocr call made while it runs.
STEPS = frozenset(
    {
        "stirling.extract_text",
        "stirling.ocr",
        "stirling.convert",
        "ai.generate",
        "ai.warmup",
        "forms.inspect",
        "forms.fill",
    }
)

ERROR_CODES = frozenset(STATUS_BY_CODE)
UNKNOWN_ERROR = "unknown"
# Step outcome: "ok", a DocPipe error code, or "error" (any other exception).
OUTCOME_OK = "ok"
OUTCOME_ERROR = "error"

# Error codes that point at an upstream, and which upstream.
UPSTREAM_BY_ERROR: dict[str, str] = {
    "upstream_unavailable": "stirling",
    "upstream_auth_failed": "stirling",
    "processing_failed": "stirling",
    "timeout": "stirling",
    "conversion_failed": "stirling",
    "ai_unavailable": "ollama",
    "ai_timeout": "ollama",
    "ai_invalid_response": "ollama",
    "ai_processing_failed": "ollama",
    "model_profile_unavailable": "docpipe",
}

SEVERITIES = ("INFO", "WARNING", "ERROR")
EVENT_SERVICES = frozenset({"docpipe", "ollama", "stirling", "monitoring", "system"})

# Structured technical events: code -> severity.
EVENT_CODES: dict[str, str] = {
    "monitoring_started": "INFO",
    "monitoring_stopped": "INFO",
    "upstream_error": "WARNING",
    "unhandled_exception": "ERROR",
    "service_unreachable": "WARNING",
    "service_recovered": "INFO",
    "buffer_overflow": "WARNING",
    "store_unavailable": "ERROR",
    "store_recovered": "INFO",
    "storage_pressure": "WARNING",
    "storage_limit_reached": "ERROR",
    "disk_space_low": "ERROR",
    "collector_task_failed": "ERROR",
    "hook_failed": "WARNING",
}

COLLECTOR_TASKS = frozenset({"flush", "sample", "probe", "maintenance"})

# System metric names (system_samples.metric) and their sources.
SOURCES = frozenset({"host", "container", "process", "filesystem", "network_namespace", "gpu", "monitoring"})
SAMPLE_STATUSES = frozenset({"ok", "unavailable", "not_applicable"})
SAMPLE_REASONS = frozenset(
    {
        "",
        "not_supported_platform",
        "cgroup_v2_not_found",
        "not_configured",
        "read_error",
        "warming_up",
        "unlimited",
    }
)

PROBE_SERVICES = frozenset({"ollama", "stirling"})
PROBE_STATUSES = frozenset(
    {
        "ok",
        "not_configured",
        "timeout",
        "connection_error",
        "http_error",
        "invalid_response",
    }
)

# Ids from the config / upstream metadata: printable, bounded, no whitespace
# or control characters. Checked in addition to registry membership.
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,199}")
_VERSION = re.compile(r"[0-9A-Za-z][0-9A-Za-z.+_\-]{0,39}")


_ROUTE = re.compile(r"/[A-Za-z0-9_.{}:/\-]{0,200}")


def safe_route(value: object) -> bool:
    return isinstance(value, str) and bool(_ROUTE.fullmatch(value))


def safe_id(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _ID.fullmatch(value) else None


def safe_version(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _VERSION.fullmatch(value) else None


def member(value: object, allowed: frozenset[str], fallback: str) -> str:
    return value if isinstance(value, str) and value in allowed else fallback


def error_code(value: object) -> str | None:
    if value is None:
        return None
    return member(value, ERROR_CODES, UNKNOWN_ERROR)


def outcome(value: object) -> str:
    if value == OUTCOME_OK:
        return OUTCOME_OK
    return member(value, ERROR_CODES, OUTCOME_ERROR)
