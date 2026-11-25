"""Authenticated capability discovery for the calling client."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from system.ai.modes import MODES
from system.assistant.domains import ASSISTANT_DOMAINS
from system.assistant.modes import ASSISTANT_MODES
from system.auth import get_authenticated_client
from system.config import ClientConfig, Settings
from system.forms.inspect import SCHEMA_VERSION as FORMS_SCHEMA_VERSION
from system.schemas import (
    CapabilitiesResponse,
    FormsCapabilitiesResponse,
    FormsCapabilityInfo,
    FormsServiceFlags,
    ServiceFlags,
)

router = APIRouter()


@router.get("/api/v1/capabilities", response_model=CapabilitiesResponse)
def capabilities(
    request: Request, client: ClientConfig = Depends(get_authenticated_client)
) -> CapabilitiesResponse | JSONResponse:
    settings: Settings = request.app.state.settings

    features = []
    if client.services.documents:
        features.append("extract_text")
        features.append("convert_to_pdf")
        if settings.stirling.ocr.enabled:
            features.append("ocr_fallback")
    if client.services.ai:
        features.append("analyze")
    if client.services.prompt_lab:
        features.append("prompt_lab")
    if client.services.assistant:
        features.append("assistant")

    response = CapabilitiesResponse(
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
        assistant_domains=list(ASSISTANT_DOMAINS.keys()) if client.services.assistant else None,
    )
    if not client.services.forms:
        # Unchanged shape for every client without the forms flag.
        return response

    # Phase 2.1a: a superset, returned directly so the declared
    # response_model (CapabilitiesResponse) cannot strip the added keys.
    base = response.model_dump()
    base_services = base.pop("services")
    base["features"] = base["features"] + ["forms_inspect"]
    forms_response = FormsCapabilitiesResponse(
        **base,
        services=FormsServiceFlags(**base_services, forms=True),
        forms=FormsCapabilityInfo(
            schema_version=FORMS_SCHEMA_VERSION,
            max_pages=settings.forms.max_pages,
            max_fields=settings.forms.max_fields,
        ),
    )
    return JSONResponse(content=forms_response.model_dump())
