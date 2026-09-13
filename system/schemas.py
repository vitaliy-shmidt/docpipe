"""Response schemas for the DocPipe API.

DocPipe deliberately exposes a small, stable set of generic fields.
Clients must never need to know anything about the upstream document
processing engine (Stirling) to consume these responses.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
    # Only populated when services.ai is true for the calling client -
    # lets a consumer discover valid `mode` values without hardcoding them.
    ai_modes: list[str] | None = None


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


class AnalyzeRequest(BaseModel):
    # extra="forbid": the API boundary is mode/context/text ONLY. A client
    # sending model/model_profile/provider/temperature/timeout/
    # system_prompt/prompt must get a rejected request (invalid_request),
    # never a silently-ignored field that might look like it worked.
    model_config = ConfigDict(extra="forbid")

    mode: str
    # Open, generic bag of hints (e.g. hotel_name, asset_name, taxonomy,
    # known_vendor) - not all fields need to be present, and DocPipe does
    # not interpret any specific key itself; it is passed through to the
    # prompt as disambiguation context only.
    context: dict[str, Any] = Field(default_factory=dict)
    text: str


class AnalyzeData(BaseModel):
    mode: str
    # The profile NAME (e.g. "light"), not the model - useful for later
    # benchmarking/telemetry without hardcoding a model string anywhere
    # that reads this response.
    model_profile: str
    model: str
    prompt_version: str
    result: dict[str, Any]


class AnalyzeResponse(BaseModel):
    ok: bool
    data: AnalyzeData
