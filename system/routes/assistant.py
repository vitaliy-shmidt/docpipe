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

from fastapi import APIRouter, Depends, Request

from system.ai.resolver import resolve_model_profile
from system.assistant.modes import ANSWER_SCHEMA, get_assistant_mode
from system.assistant.prompting import build_assistant_prompt
from system.assistant.router import route
from system.auth import get_authenticated_client, require_assistant_service
from system.config import ClientConfig
from system.errors import DocPipeError
from system.schemas import AssistantQueryData, AssistantQueryRequest, AssistantQueryResponse

router = APIRouter()
logger = logging.getLogger("docpipe.assistant")

# Task §54: bounded, not tuned - a real question is a short sentence; a
# real context payload is a prepared summary, not a database dump.
MAX_QUESTION_LENGTH = 5_000
MAX_CONTEXT_SERIALIZED_LENGTH = 100_000


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

    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "assistant_mode": mode.name,
        "matched_rule": assistant_route.matched_rule,
        "model_profile": profile_name,
        "model": profile.model,
        "prompt_version": prompt_version,
        "question_length": len(question),
    }
    started_at = time.monotonic()
    try:
        result = request.app.state.ollama_client.generate_structured(
            prompt,
            ANSWER_SCHEMA,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            temperature=profile.temperature,
        )
    except DocPipeError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logger.info(
            "request_id=%(request_id)s client_id=%(client_id)s assistant_mode=%(assistant_mode)s "
            "matched_rule=%(matched_rule)s model_profile=%(model_profile)s model=%(model)s "
            "prompt_version=%(prompt_version)s question_length=%(question_length)s "
            "status=%(status)s duration_ms=%(duration_ms)s",
            {**log_fields, "status": exc.code, "duration_ms": duration_ms},
        )
        raise

    duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        "request_id=%(request_id)s client_id=%(client_id)s assistant_mode=%(assistant_mode)s "
        "matched_rule=%(matched_rule)s model_profile=%(model_profile)s model=%(model)s "
        "prompt_version=%(prompt_version)s question_length=%(question_length)s "
        "status=success duration_ms=%(duration_ms)s",
        {**log_fields, "duration_ms": duration_ms},
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
