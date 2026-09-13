"""Authenticated capability discovery for the calling client."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from system.ai.modes import MODES
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

    return CapabilitiesResponse(
        ok=True,
        services=ServiceFlags(documents=client.services.documents, ai=client.services.ai),
        features=features,
        ai_modes=list(MODES.keys()) if client.services.ai else None,
    )
