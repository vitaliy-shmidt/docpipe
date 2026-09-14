"""AI analysis endpoint (V2).

Deliberately separate from /documents/extract-text (see README "Pipeline"
section): this endpoint takes already-extracted text, never touches
Stirling, and never re-runs text extraction. A consumer stores the result
of extract-text first; if analyze later fails, the already-extracted text
is untouched and does not need to be re-extracted.

Architecture principle: the client selects a *mode* (a task); DocPipe -
never the client - selects the *model*, via system/ai/resolver.py. The
request schema (AnalyzeRequest) has no model/model_profile/provider/
temperature/timeout/prompt field at all, so there is nothing for a client
to override even if it tried - not even a prompt, ever (see task V2.2
requirement: a draft prompt is exclusively a /api/v1/lab/analyze thing,
system/routes/lab.py; this endpoint only ever runs the mode's currently
*active*, server-controlled prompt version).

The prompt itself is resolved fresh on every request via
system/ai/prompt_registry.py (mode -> active version -> file content) -
never cached, never baked in at import time, so activating a new version
through the Prompt Lab takes effect on the very next call here with no
restart (see docs/prompt-lab.md).
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request

from system.ai.modes import get_mode
from system.ai.prompting import build_prompt
from system.ai.resolver import resolve_model_profile
from system.auth import get_authenticated_client, require_ai_service
from system.config import ClientConfig
from system.errors import DocPipeError
from system.schemas import AnalyzeData, AnalyzeRequest, AnalyzeResponse

router = APIRouter()
logger = logging.getLogger("docpipe.ai")


@router.post("/api/v1/documents/analyze", response_model=AnalyzeResponse)
def analyze(
    request: Request,
    body: AnalyzeRequest,
    client: ClientConfig = Depends(get_authenticated_client),
) -> AnalyzeResponse:
    require_ai_service(client)

    mode = get_mode(body.mode)
    if mode is None:
        raise DocPipeError("unknown_mode", f"Unknown analysis mode: {body.mode!r}.")

    if len(body.text) > mode.max_input_length:
        raise DocPipeError(
            "input_too_large",
            f"Input text exceeds the maximum length of {mode.max_input_length} characters for this mode.",
        )

    settings = request.app.state.settings
    profile_name, profile = resolve_model_profile(mode, client, settings)

    registry = request.app.state.prompt_registry
    prompt_version = registry.resolve_active_version(mode.name, mode.default_prompt_version)
    prompt_template = registry.load_prompt(mode.name, prompt_version)
    prompt = build_prompt(prompt_template, body.context, body.text)

    # Same request_id as the generic access-log line (main.py middleware),
    # for correlation - but with the AI-specific dimensions that line
    # doesn't carry. Never the document text, the prompt, or the model
    # output itself.
    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "mode": mode.name,
        "model_profile": profile_name,
        "model": profile.model,
        "prompt_version": prompt_version,
        "input_length": len(body.text),
    }
    started_at = time.monotonic()
    try:
        result = request.app.state.ollama_client.generate_structured(
            prompt,
            mode.response_schema,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            temperature=profile.temperature,
        )
    except DocPipeError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logger.info(
            "request_id=%(request_id)s client_id=%(client_id)s mode=%(mode)s "
            "model_profile=%(model_profile)s model=%(model)s prompt_version=%(prompt_version)s "
            "input_length=%(input_length)s status=%(status)s duration_ms=%(duration_ms)s",
            {**log_fields, "status": exc.code, "duration_ms": duration_ms},
        )
        raise

    duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        "request_id=%(request_id)s client_id=%(client_id)s mode=%(mode)s "
        "model_profile=%(model_profile)s model=%(model)s prompt_version=%(prompt_version)s "
        "input_length=%(input_length)s status=success duration_ms=%(duration_ms)s",
        {**log_fields, "duration_ms": duration_ms},
    )

    return AnalyzeResponse(
        ok=True,
        data=AnalyzeData(
            mode=mode.name,
            model_profile=profile_name,
            model=profile.model,
            prompt_version=prompt_version,
            result=result,
        ),
    )
