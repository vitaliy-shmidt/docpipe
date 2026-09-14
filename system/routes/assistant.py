"""Assistant query endpoint (V2.2 foundation) - see docs/assistant-routing.md.

    question -> system/assistant/router.py -> AssistantMode
             -> system/ai/resolver.py -> ModelProfile (reused, unchanged)
             -> system/ai/prompt_registry.py -> active prompt version/content
             -> system/assistant/prompting.py -> final prompt
             -> OllamaClient.generate_structured() (reused, unchanged)
             -> answer

`context` is trusted, server-prepared input - HubDix (or any other
caller) is responsible for permissions/hotel-scope/actually fetching the
data before calling this endpoint; DocPipe never queries a database, never
generates SQL, and never calls out to HubDix or any other system on its
own (see module docstring of system/assistant/__init__.py). This endpoint
does not implement a full Hotel AI Assistant - it is routing + prompting
only, the foundation a later pass builds the real thing on.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Body, Depends, Request

from system.ai.resolver import resolve_model_profile
from system.assistant.modes import ANSWER_SCHEMA, get_assistant_mode
from system.assistant.prompting import build_assistant_prompt
from system.assistant.router import route
from system.auth import get_authenticated_client, require_assistant_service
from system.config import ClientConfig
from system.errors import DocPipeError
from system.schemas import (
    AssistantQueryData,
    AssistantQueryRequest,
    AssistantQueryResponse,
    AssistantWarmupData,
    AssistantWarmupRequest,
    AssistantWarmupResponse,
)

router = APIRouter()
logger = logging.getLogger("docpipe.assistant")

# Task §54: bounded, not tuned - a real question is a short sentence; a
# real context payload is a prepared summary, not a database dump.
MAX_QUESTION_LENGTH = 5_000
MAX_CONTEXT_SERIALIZED_LENGTH = 100_000

# Warm-up mode: the cockpit's Hotel Health card is the only caller today, so
# warming the model it uses is the useful thing to pre-load (task §36 - not
# every assistant mode, just this one).
WARMUP_MODE_NAME = "hotel_health_summary"

# A cold load can take much longer than a normal generate call, but must
# still be bounded - never below the mode's own configured timeout, never
# above a hard ceiling (task §15).
WARMUP_MIN_TIMEOUT_SECONDS = 120.0
WARMUP_MAX_TIMEOUT_SECONDS = 180.0


@router.post("/api/v1/assistant/query", response_model=AssistantQueryResponse)
def assistant_query(
    request: Request,
    body: AssistantQueryRequest,
    client: ClientConfig = Depends(get_authenticated_client),
) -> AssistantQueryResponse:
    require_assistant_service(client)

    question = body.question.strip()
    if not question:
        raise DocPipeError("invalid_request", "question must not be empty.")
    if len(question) > MAX_QUESTION_LENGTH:
        raise DocPipeError(
            "input_too_large", f"question exceeds the maximum length of {MAX_QUESTION_LENGTH} characters."
        )
    # json.dumps mirrors exactly how the context is rendered into the
    # prompt (system/assistant/prompting.py) - the length check measures
    # what actually reaches the model, not some unrelated approximation.
    serialized_context = json.dumps(body.context, ensure_ascii=False, default=str)
    if len(serialized_context) > MAX_CONTEXT_SERIALIZED_LENGTH:
        raise DocPipeError(
            "input_too_large",
            f"context exceeds the maximum serialized length of {MAX_CONTEXT_SERIALIZED_LENGTH} characters.",
        )

    assistant_route = route(question)
    mode = get_assistant_mode(assistant_route.mode)
    assert mode is not None  # router only ever returns a name that exists in ASSISTANT_MODES

    settings = request.app.state.settings
    profile_name, profile = resolve_model_profile(mode, client, settings)

    registry = request.app.state.prompt_registry
    subdir = f"assistant/{mode.name}"
    prompt_version = registry.resolve_active_version(subdir, mode.default_prompt_version)
    prompt_template = registry.load_prompt(subdir, prompt_version)
    prompt = build_assistant_prompt(prompt_template, body.context, question)

    # Timing Metrics pass (task §11-§14): metadata-only, never question/
    # context/answer/prompt content. `timing` is populated in place by
    # OllamaClient.generate_structured() (system/services/ollama.py) with
    # ollama_duration_ms (+ optional load/eval breakdown) - a single shared
    # mechanism also used by /documents/analyze below, not a second
    # assistant-specific timing implementation.
    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "assistant_mode": mode.name,
        "matched_rule": assistant_route.matched_rule,
        "model_profile": profile_name,
        "model": profile.model,
        "prompt_version": prompt_version,
        "question_chars": len(question),
        "context_chars": len(serialized_context),
    }
    timing: dict = {}
    started_at = time.monotonic()
    try:
        result = request.app.state.ollama_client.generate_structured(
            prompt,
            ANSWER_SCHEMA,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            temperature=profile.temperature,
            timing=timing,
        )
    except DocPipeError as exc:
        total_duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logger.info(
            "request_id=%(request_id)s client_id=%(client_id)s assistant_mode=%(assistant_mode)s "
            "matched_rule=%(matched_rule)s model_profile=%(model_profile)s model=%(model)s "
            "prompt_version=%(prompt_version)s question_chars=%(question_chars)s "
            "context_chars=%(context_chars)s status=%(status)s "
            "ollama_duration_ms=%(ollama_duration_ms)s total_duration_ms=%(total_duration_ms)s",
            {
                **log_fields,
                "status": exc.code,
                "ollama_duration_ms": timing.get("ollama_duration_ms"),
                "total_duration_ms": total_duration_ms,
            },
        )
        raise

    total_duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        "request_id=%(request_id)s client_id=%(client_id)s assistant_mode=%(assistant_mode)s "
        "matched_rule=%(matched_rule)s model_profile=%(model_profile)s model=%(model)s "
        "prompt_version=%(prompt_version)s question_chars=%(question_chars)s "
        "context_chars=%(context_chars)s status=success "
        "ollama_duration_ms=%(ollama_duration_ms)s total_duration_ms=%(total_duration_ms)s",
        {
            **log_fields,
            "ollama_duration_ms": timing.get("ollama_duration_ms"),
            "total_duration_ms": total_duration_ms,
        },
    )

    return AssistantQueryResponse(
        ok=True,
        data=AssistantQueryData(
            assistant_mode=mode.name,
            matched_rule=assistant_route.matched_rule,
            model_profile=profile_name,
            model=profile.model,
            prompt_version=prompt_version,
            answer=result["answer"],
        ),
    )


@router.post("/api/v1/assistant/warmup", response_model=AssistantWarmupResponse)
def assistant_warmup(
    request: Request,
    body: AssistantWarmupRequest = Body(default_factory=AssistantWarmupRequest),
    client: ClientConfig = Depends(get_authenticated_client),
) -> AssistantWarmupResponse:
    """Load the cockpit assistant's model into Ollama, no question/context.

    Purely an infra optimization: the caller gets no answer and no context
    is read or required (task §7, §21) - it only makes the *next* real
    /assistant/query cheaper. Reuses the exact same permission gate and
    model resolver /assistant/query uses, so the warmed model is always the
    one that mode would actually run.
    """
    require_assistant_service(client)

    mode = get_assistant_mode(WARMUP_MODE_NAME)
    assert mode is not None  # WARMUP_MODE_NAME is a fixed, known-good ASSISTANT_MODES key

    settings = request.app.state.settings
    profile_name, profile = resolve_model_profile(mode, client, settings)
    timeout_seconds = min(
        max(profile.timeout_seconds, WARMUP_MIN_TIMEOUT_SECONDS), WARMUP_MAX_TIMEOUT_SECONDS
    )

    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "model_profile": profile_name,
        "model": profile.model,
    }
    timing: dict = {}
    started_at = time.monotonic()
    try:
        request.app.state.ollama_client.warm_up(
            model=profile.model, timeout_seconds=timeout_seconds, timing=timing
        )
    except DocPipeError as exc:
        total_duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logger.info(
            "request_id=%(request_id)s client_id=%(client_id)s model_profile=%(model_profile)s "
            "model=%(model)s status=%(status)s ollama_duration_ms=%(ollama_duration_ms)s "
            "total_duration_ms=%(total_duration_ms)s",
            {
                **log_fields,
                "status": exc.code,
                "ollama_duration_ms": timing.get("ollama_duration_ms"),
                "total_duration_ms": total_duration_ms,
            },
        )
        raise

    total_duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        "request_id=%(request_id)s client_id=%(client_id)s model_profile=%(model_profile)s "
        "model=%(model)s status=success ollama_duration_ms=%(ollama_duration_ms)s "
        "total_duration_ms=%(total_duration_ms)s",
        {
            **log_fields,
            "ollama_duration_ms": timing.get("ollama_duration_ms"),
            "total_duration_ms": total_duration_ms,
        },
    )

    return AssistantWarmupResponse(
        ok=True,
        data=AssistantWarmupData(
            ready=True,
            model_profile=profile_name,
            model=profile.model,
            duration_ms=total_duration_ms,
        ),
    )
