"""Prompt Lab (V2.2) - see docs/prompt-lab.md.

Lets a `prompt_lab` client list/inspect a mode's prompt versions, test an
arbitrary *draft* prompt against the mode's real model routing/schema/
repair mechanism without touching anything persisted, save a new
(immutable) version, and explicitly activate one. None of this is
reachable without the `prompt_lab` client permission (system/auth.py) -
a plain `ai` client can call /documents/analyze but has no visibility
into prompt versions or a way to influence which one is active.

Every route here is a thin wrapper: mode/version validation and model
routing are delegated to the exact same system/ai/modes.py,
system/ai/resolver.py, and system/ai/prompt_registry.py that
system/routes/analyze.py uses - there is no second, parallel prompt/model
resolution implementation for the Lab.
"""

from __future__ import annotations

import hashlib
import logging

from fastapi import APIRouter, Depends, Query, Request

from system.ai.modes import MODES, get_mode
from system.ai.prompting import build_prompt, validate_prompt_template
from system.ai.resolver import resolve_model_profile
from system.auth import get_authenticated_client, require_ai_service, require_prompt_lab_service
from system.config import ClientConfig
from system.errors import DocPipeError
from system.schemas import (
    ActivatePromptVersionData,
    ActivatePromptVersionRequest,
    ActivatePromptVersionResponse,
    LabAnalyzeData,
    LabAnalyzeRequest,
    LabAnalyzeResponse,
    PromptListData,
    PromptListResponse,
    PromptLoadData,
    PromptLoadResponse,
    PromptModeSummary,
    SavePromptVersionData,
    SavePromptVersionRequest,
    SavePromptVersionResponse,
)

router = APIRouter()
logger = logging.getLogger("docpipe.lab")


def _get_known_mode(mode_name: str):
    mode = get_mode(mode_name)
    if mode is None:
        raise DocPipeError("unknown_mode", f"Unknown analysis mode: {mode_name!r}.")
    return mode


def _log(request: Request, client: ClientConfig, action: str, mode: str, version: str | None = None) -> None:
    # Metadata only (task §22) - never prompt content, document text, or
    # the API key.
    logger.info(
        "request_id=%s client_id=%s action=%s mode=%s version=%s",
        getattr(request.state, "request_id", None),
        client.client_id,
        action,
        mode,
        version,
    )


@router.get("/api/v1/lab/prompts", response_model=PromptListResponse)
def list_prompts(
    request: Request, client: ClientConfig = Depends(get_authenticated_client)
) -> PromptListResponse:
    require_prompt_lab_service(client)
    registry = request.app.state.prompt_registry

    modes = []
    for mode in MODES.values():
        active_version = registry.resolve_active_version(mode.name, mode.default_prompt_version)
        modes.append(
            PromptModeSummary(
                mode=mode.name,
                active_version=active_version,
                versions=registry.list_versions(mode.name),
            )
        )
    _log(request, client, "list", mode="*")
    return PromptListResponse(ok=True, data=PromptListData(modes=modes))


@router.get("/api/v1/lab/prompts/{mode}", response_model=PromptLoadResponse)
def load_prompt(
    mode: str,
    request: Request,
    version: str | None = Query(default=None),
    client: ClientConfig = Depends(get_authenticated_client),
) -> PromptLoadResponse:
    require_prompt_lab_service(client)
    known_mode = _get_known_mode(mode)
    registry = request.app.state.prompt_registry

    active_version = registry.resolve_active_version(known_mode.name, known_mode.default_prompt_version)
    resolved_version = version or active_version
    content = registry.load_prompt(known_mode.name, resolved_version)

    _log(request, client, "load", known_mode.name, resolved_version)
    return PromptLoadResponse(
        ok=True,
        data=PromptLoadData(
            mode=known_mode.name,
            version=resolved_version,
            active=resolved_version == active_version,
            content=content,
        ),
    )


@router.post("/api/v1/lab/analyze", response_model=LabAnalyzeResponse)
def draft_analyze(
    request: Request,
    body: LabAnalyzeRequest,
    client: ClientConfig = Depends(get_authenticated_client),
) -> LabAnalyzeResponse:
    # Both required (module docstring): prompt_lab for Lab access at all,
    # ai because this is the one Lab route that actually calls Ollama.
    require_prompt_lab_service(client)
    require_ai_service(client)

    mode = _get_known_mode(body.mode)
    if len(body.text) > mode.max_input_length:
        raise DocPipeError(
            "input_too_large",
            f"Input text exceeds the maximum length of {mode.max_input_length} characters for this mode.",
        )
    if not body.prompt.strip():
        raise DocPipeError("invalid_prompt", "Draft prompt must not be empty.")

    settings = request.app.state.settings
    if len(body.prompt) > settings.prompts.max_content_length:
        raise DocPipeError(
            "prompt_too_large",
            f"Draft prompt exceeds the maximum length of {settings.prompts.max_content_length} characters.",
        )

    profile_name, profile = resolve_model_profile(mode, client, settings)
    # Never the mode's active/persisted prompt - always exactly the draft
    # the client just sent (task §16/§17: this call must never touch or
    # be influenced by the active version at all).
    prompt = build_prompt(body.prompt, body.context, body.text)

    _log(request, client, "draft_analyze", mode.name)
    result = request.app.state.ollama_client.generate_structured(
        prompt,
        mode.response_schema,
        model=profile.model,
        timeout_seconds=profile.timeout_seconds,
        temperature=profile.temperature,
    )

    return LabAnalyzeResponse(
        ok=True,
        data=LabAnalyzeData(
            mode=mode.name,
            prompt_source="draft",
            prompt_sha256=hashlib.sha256(body.prompt.encode("utf-8")).hexdigest(),
            model_profile=profile_name,
            model=profile.model,
            result=result,
        ),
    )


@router.post("/api/v1/lab/prompts/{mode}/versions", response_model=SavePromptVersionResponse)
def save_prompt_version(
    mode: str,
    request: Request,
    body: SavePromptVersionRequest,
    client: ClientConfig = Depends(get_authenticated_client),
) -> SavePromptVersionResponse:
    require_prompt_lab_service(client)
    known_mode = _get_known_mode(mode)
    # Fail fast on a syntactically broken template (task §19/§20 - never
    # persist a version that /documents/analyze would later choke on) -
    # save_version() below re-checks emptiness/size itself; this call
    # covers the one thing it can't check: whether {context_block}/{text}
    # actually render.
    validate_prompt_template(body.content)

    registry = request.app.state.prompt_registry
    registry.save_version(known_mode.name, body.version, body.content)

    _log(request, client, "save_version", known_mode.name, body.version)
    return SavePromptVersionResponse(
        ok=True, data=SavePromptVersionData(mode=known_mode.name, version=body.version)
    )


@router.post("/api/v1/lab/prompts/{mode}/activate", response_model=ActivatePromptVersionResponse)
def activate_prompt_version(
    mode: str,
    request: Request,
    body: ActivatePromptVersionRequest,
    client: ClientConfig = Depends(get_authenticated_client),
) -> ActivatePromptVersionResponse:
    require_prompt_lab_service(client)
    known_mode = _get_known_mode(mode)

    registry = request.app.state.prompt_registry
    registry.activate_version(known_mode.name, body.version)

    _log(request, client, "activate", known_mode.name, body.version)
    return ActivatePromptVersionResponse(
        ok=True, data=ActivatePromptVersionData(mode=known_mode.name, version=body.version)
    )
