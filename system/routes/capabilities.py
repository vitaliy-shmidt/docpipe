"""Authenticated capability discovery for the calling client."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from system.ai.modes import MODES
from system.assistant.modes import ASSISTANT_MODES
from system.auth import get_authenticated_client
from system.config import ClientConfig, Settings
from system.schemas import CapabilitiesResponse, ServiceFlags

router = APIRouter()


@router.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    request: Request, client: ClientConfig = Depends(get_authenticated_client)
) -> CapabilitiesResponse:
    settings: Settings = request.app.state.settings

    features = []
    if client.services.documents:
        features.append("extract_text")
        if settings.stirling.ocr.enabled:
            features.append("ocr_fallback")
    if client.services.ai:
        features.append("analyze")
    if client.services.prompt_lab:
        features.append("prompt_lab")
    if client.services.assistant:
        features.append("assistant")

    return CapabilitiesResponse(
        ok=True,
        services=ServiceFlags(
            documents=client.services.documents,
            ai=client.services.ai,
            prompt_lab=client.services.prompt_lab,
            assistant=client.services.assistant,
        ),
        features=features,
        ai_modes=list(MODES.keys()) if client.services.ai else None,
        assistant_modes=list(ASSISTANT_MODES.keys()) if client.services.assistant else None,
    )
