"""Unauthenticated liveness check. Does not contact Stirling."""

from __future__ import annotations

from fastapi import APIRouter

from system import VERSION
from system.schemas import HealthResponse

router = APIRouter()


@router.get("/api/v1/health", response_model=HealthResponse)
def health() -> HealthResponse:
    return HealthResponse(ok=True, service="docpipe", version=VERSION)
