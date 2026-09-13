"""AI analysis endpoint (V2).

Deliberately separate from /documents/extract-text (see README "Pipeline"
section): this endpoint takes already-extracted text, never touches
Stirling, and never re-runs text extraction. A consumer stores the result
of extract-text first; if analyze later fails, the already-extracted text
is untouched and does not need to be re-extracted.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, Depends, Request

from system.ai.modes import get_mode
from system.ai.prompting import build_prompt
from system.auth import get_authenticated_client, require_ai_service
from system.config import ClientConfig, Settings
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

    settings: Settings = request.app.state.settings
    prompt = build_prompt(mode, body.context, body.text)

    # Same request_id as the generic access-log line (main.py middleware),
    # for correlation - but with the AI-specific dimensions that line
    # doesn't carry. Never the document text, the prompt, or the model
    # output itself.
    log_fields = {
        "request_id": getattr(request.state, "request_id", None),
        "client_id": client.client_id,
        "mode": mode.name,
        "model": settings.ollama.model,
        "input_length": len(body.text),
    }
    started_at = time.monotonic()
    try:
        result = request.app.state.ollama_client.generate_structured(prompt, mode.response_schema)
    except DocPipeError as exc:
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)
        logger.info(
            "request_id=%(request_id)s client_id=%(client_id)s mode=%(mode)s model=%(model)s "
            "input_length=%(input_length)s status=%(status)s duration_ms=%(duration_ms)s",
            {**log_fields, "status": exc.code, "duration_ms": duration_ms},
        )
        raise

    duration_ms = round((time.monotonic() - started_at) * 1000, 1)
    logger.info(
        "request_id=%(request_id)s client_id=%(client_id)s mode=%(mode)s model=%(model)s "
        "input_length=%(input_length)s status=success duration_ms=%(duration_ms)s",
        {**log_fields, "duration_ms": duration_ms},
    )

    return AnalyzeResponse(
        ok=True,
        data=AnalyzeData(
            mode=mode.name,
            prompt_version=mode.prompt_version,
            model=settings.ollama.model,
            result=result,
        ),
    )
