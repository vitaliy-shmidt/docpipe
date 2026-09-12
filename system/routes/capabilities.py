"""Authenticated capability discovery for the calling client."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from system.auth import get_authenticated_client
from system.config import ClientConfig
from system.schemas import CapabilitiesResponse, ServiceFlags

router = APIRouter()


@router.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
def capabilities(client: ClientConfig = Depends(get_authenticated_client)) -> CapabilitiesResponse:
    features = ["extract_text"] if client.services.documents else []
    return CapabilitiesResponse(
        ok=True,
        services=ServiceFlags(documents=client.services.documents, ai=False),
        features=features,
    )
