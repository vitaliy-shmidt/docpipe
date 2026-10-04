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
from system.ai.warmup import run_warmup
from system.assistant.domains import resolve_assistant_route, validate_domains, warmup_mode_for_domains
from system.assistant.modes import ANSWER_SCHEMA, get_assistant_mode
from system.assistant.prompting import build_assistant_prompt
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
# Assistant V2: a SHORT history only - the consumer keeps structured state
# (selected entity etc.) and re-sends fresh context every turn, so the
# model never needs a long transcript. Bounded per turn and in total.
MAX_HISTORY_TURNS = 8
MAX_HISTORY_TURN_LENGTH = 2_000

# Warm-up mode: the cockpit's Hotel Health card is the only caller today, so
# warming the model it uses is the useful thing to pre-load (task §36 - not
# every assistant mode, just this one).
WARMUP_MODE_NAME = "hotel_health_summary"



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

    domains = validate_domains(body.domains)
    if len(body.history) > MAX_HISTORY_TURNS:
        raise DocPipeError("input_too_large", f"history exceeds {MAX_HISTORY_TURNS} messages.")
    history = []
    for turn in body.history:
        if len(turn.content) > MAX_HISTORY_TURN_LENGTH:
            raise DocPipeError(
                "input_too_large", f"a history message exceeds {MAX_HISTORY_TURN_LENGTH} characters."
            )
        history.append({"role": turn.role, "content": turn.content})

    assistant_route = resolve_assistant_route(question, domains)
    mode = get_assistant_mode(assistant_route.mode)
    assert mode is not None  # router only ever returns a name that exists in ASSISTANT_MODES

    settings = request.app.state.settings
    profile_name, profile = resolve_model_profile(mode, client, settings)

    registry = request.app.state.prompt_registry
    subdir = f"assistant/{mode.name}"
    prompt_version = registry.resolve_active_version(subdir, mode.default_prompt_version)
    prompt_template = registry.load_prompt(subdir, prompt_version)
    prompt = build_assistant_prompt(prompt_template, body.context, question, history)

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
        "domains": "+".join(domains) or "-",
        "history_turns": len(history),
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
            "context_chars=%(context_chars)s domains=%(domains)s history_turns=%(history_turns)s "
            "status=%(status)s "
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
        "context_chars=%(context_chars)s domains=%(domains)s history_turns=%(history_turns)s status=success "
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

    # Assistant V2: optional domains pick the mode a conversation in those
    # domains starts with (system/assistant/domains.py); none = the cockpit's
    # hotel_health_summary exactly as before.
    domains = validate_domains(body.domains)
    mode = get_assistant_mode(warmup_mode_for_domains(domains, WARMUP_MODE_NAME))
    assert mode is not None  # always a known ASSISTANT_MODES key

    # Same shared helper as /api/v1/documents/warmup (system/ai/warmup.py) -
    # one warm-up implementation, two task-specific entry points.
    data = run_warmup(request, client, mode, logger)
    return AssistantWarmupResponse(ok=True, data=AssistantWarmupData(**data))
