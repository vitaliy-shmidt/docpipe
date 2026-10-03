"""Shared model warm-up: load the model a mode resolves to into Ollama.

Purely an infra optimization - no prompt, no document, no answer. Used by
/api/v1/assistant/warmup (fixed assistant mode, see system/routes/
assistant.py) and /api/v1/documents/warmup (a named extraction mode, see
system/routes/analyze.py). Both go through the same resolver as the real
request they prepare, so the warmed model is always the one that mode would
actually run for this client (incl. its model_overrides) - the caller names
a task, never a model.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request

from system.ai.resolver import _ModeLike, resolve_model_profile
from system.config import ClientConfig
from system.errors import DocPipeError

# A cold load can take much longer than a normal generate call, but must
# still be bounded - never below the mode's own configured timeout, never
# above a hard ceiling.
WARMUP_MIN_TIMEOUT_SECONDS = 120.0
WARMUP_MAX_TIMEOUT_SECONDS = 180.0


def run_warmup(request: Request, client: ClientConfig, mode: _ModeLike, logger: logging.Logger) -> dict:
    """Warms the mode's resolved model. Returns the response `data` dict.

    Raises DocPipeError (ai_unavailable/ai_timeout/...) exactly like the
    real request path would; logs metadata only (never content).
    """
    settings = request.app.state.settings
    profile_name, profile = resolve_model_profile(mode, client, settings)
    timeout_seconds = min(
        max(profile.timeout_seconds, WARMUP_MIN_TIMEOUT_SECONDS), WARMUP_MAX_TIMEOUT_SECONDS
    )

    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "mode": mode.name,
        "model_profile": profile_name,
        "model": profile.model,
    }
    log_format = (
        "request_id=%(request_id)s client_id=%(client_id)s action=warmup mode=%(mode)s "
        "model_profile=%(model_profile)s model=%(model)s status=%(status)s "
        "ollama_duration_ms=%(ollama_duration_ms)s total_duration_ms=%(total_duration_ms)s"
    )
    timing: dict = {}
    started_at = time.monotonic()
    try:
        request.app.state.ollama_client.warm_up(
            model=profile.model, timeout_seconds=timeout_seconds, timing=timing
        )
    except DocPipeError as exc:
        logger.info(
            log_format,
            {
                **log_fields,
                "status": exc.code,
                "ollama_duration_ms": timing.get("ollama_duration_ms"),
                "total_duration_ms": round((time.monotonic() - started_at) * 1000, 1),
            },
        )
        raise

    total_duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        log_format,
        {
            **log_fields,
            "status": "success",
            "ollama_duration_ms": timing.get("ollama_duration_ms"),
            "total_duration_ms": total_duration_ms,
        },
    )
    return {
        "ready": True,
        "model_profile": profile_name,
        "model": profile.model,
        "duration_ms": total_duration_ms,
    }
