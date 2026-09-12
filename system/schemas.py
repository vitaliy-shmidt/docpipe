"""Response schemas for the DocPipe API.

DocPipe deliberately exposes a small, stable set of generic fields.
Clients must never need to know anything about the upstream document
processing engine (Stirling) to consume these responses.
"""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: bool
    service: str
    version: str


class ServiceFlags(BaseModel):
    documents: bool
    ai: bool = False


class CapabilitiesResponse(BaseModel):
    ok: bool
    services: ServiceFlags
    features: list[str]


class ExtractTextData(BaseModel):
    text: str
    text_length: int


class ExtractTextResponse(BaseModel):
    ok: bool
    data: ExtractTextData


class ErrorResponse(BaseModel):
    ok: bool = False
    code: str
    message: str
