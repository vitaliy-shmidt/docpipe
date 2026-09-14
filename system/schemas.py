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
    prompt_lab: bool = False
    assistant: bool = False


class CapabilitiesResponse(BaseModel):
    ok: bool
    services: ServiceFlags
    features: list[str]
    # Only populated when services.ai is true for the calling client -
    # lets a consumer discover valid `mode` values without hardcoding them.
    ai_modes: list[str] | None = None
    # Only populated when services.assistant is true for the calling
    # client - same discovery principle as ai_modes above.
    assistant_modes: list[str] | None = None


# Additive response metadata (see documents.py): "embedded_text" means the
# first, fast extraction attempt already had enough text; "ocr" means that
# attempt came back insufficient and the OCR fallback ran instead.
EXTRACTION_METHOD_EMBEDDED_TEXT = "embedded_text"
EXTRACTION_METHOD_OCR = "ocr"


class ExtractTextData(BaseModel):
    text: str
    text_length: int
    extraction_method: str


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


# --- Prompt Lab (V2.2) -------------------------------------------------
# See docs/prompt-lab.md. Every endpoint here requires the `prompt_lab`
# client permission (system/auth.py); /lab/analyze additionally requires
# `ai` (it is the only one that actually calls Ollama).


class PromptModeSummary(BaseModel):
    mode: str
    active_version: str
    versions: list[str]


class PromptListData(BaseModel):
    modes: list[PromptModeSummary]


class PromptListResponse(BaseModel):
    ok: bool
    data: PromptListData


class PromptLoadData(BaseModel):
    mode: str
    version: str
    active: bool
    content: str


class PromptLoadResponse(BaseModel):
    ok: bool
    data: PromptLoadData


class LabAnalyzeRequest(BaseModel):
    # extra="forbid": same closed request boundary as AnalyzeRequest - the
    # only addition here is `prompt` (the draft), and only here (never on
    # AnalyzeRequest - see task §17, enforced by AnalyzeRequest itself
    # continuing to reject an unknown `prompt` field).
    model_config = ConfigDict(extra="forbid")

    mode: str
    context: dict[str, Any] = Field(default_factory=dict)
    text: str
    prompt: str


class LabAnalyzeData(BaseModel):
    mode: str
    # Always "draft" in V2.2 - this endpoint never runs the active,
    # persisted version (that's what /documents/analyze is for); the
    # field exists so a future addition (e.g. "active") doesn't change
    # this response's shape.
    prompt_source: str
    prompt_sha256: str
    model_profile: str
    model: str
    result: dict[str, Any]


class LabAnalyzeResponse(BaseModel):
    ok: bool
    data: LabAnalyzeData


class SavePromptVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str
    content: str


class SavePromptVersionData(BaseModel):
    mode: str
    version: str


class SavePromptVersionResponse(BaseModel):
    ok: bool
    data: SavePromptVersionData


class ActivatePromptVersionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str


class ActivatePromptVersionData(BaseModel):
    mode: str
    version: str


class ActivatePromptVersionResponse(BaseModel):
    ok: bool
    data: ActivatePromptVersionData


# --- Assistant routing foundation (V2.2) --------------------------------
# See docs/assistant-routing.md. `context` is trusted, server-prepared
# input (see system/routes/assistant.py) - unlike AnalyzeRequest's
# context, this is not a bag of short disambiguation hints but the actual
# supporting data the answer must be grounded in.


class AssistantQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str
    context: dict[str, Any] = Field(default_factory=dict)


class AssistantQueryData(BaseModel):
    assistant_mode: str
    matched_rule: str
    model_profile: str
    model: str
    prompt_version: str
    answer: str


class AssistantQueryResponse(BaseModel):
    ok: bool
    data: AssistantQueryData
