"""Thin client around Ollama's HTTP API for structured JSON generation.

This is the ONLY place in DocPipe that knows anything about Ollama (its
base URL, request/response shape, and the `format`-as-JSON-Schema
structured-output feature). Route handlers never call Ollama directly and
never see a raw model response - they get back a schema-validated dict or
a normalized DocPipeError.

Verified against the current Ollama API (docs.ollama.com/api,
docs.ollama.com/capabilities/structured-outputs - introduced in v0.5.0,
Dec 2024, stable):

  POST {base_url}/api/generate
  Body: {"model": ..., "prompt": ..., "stream": false, "format": <json schema>,
         "options": {"temperature": ...}}
  Response: {"response": "<generated text, JSON-serialized>", ...}

Passing the mode's response_schema as `format` (rather than the literal
string "json") constrains Ollama's own grammar-based decoding to that
exact schema - the strongest available guarantee before DocPipe's own
schema validation runs as a second, independent check.

This client only knows the provider-wide `base_url`. Which model to run,
with what timeout and temperature, is resolved elsewhere (system/ai/
resolver.py, from the request's mode and the calling client's config) and
handed in per call - the client itself has no notion of modes, profiles,
or client overrides.
"""

from __future__ import annotations

import json

import httpx
import jsonschema

from system.errors import DocPipeError

GENERATE_PATH = "/api/generate"


class OllamaClient:
    def __init__(self, base_url: str) -> None:
        self._base_url = base_url
        self._client = httpx.Client(follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def generate_structured(
        self, prompt: str, schema: dict, *, model: str, timeout_seconds: float, temperature: float
    ) -> dict:
        """Runs prompt against `model` and validates the JSON result against schema.

        Makes exactly one repair attempt if the first response is not valid
        JSON or does not match schema. Raises DocPipeError("ai_invalid_response")
        if it is still invalid after that single retry - never loops further.
        """
        raw = self._call(
            prompt, schema, model=model, timeout_seconds=timeout_seconds, temperature=temperature
        )
        result = self._parse_and_validate(raw, schema)
        if result is not None:
            return result

        repair_prompt = self._build_repair_prompt(prompt, raw, schema)
        raw = self._call(
            repair_prompt, schema, model=model, timeout_seconds=timeout_seconds, temperature=temperature
        )
        result = self._parse_and_validate(raw, schema)
        if result is not None:
            return result

        raise DocPipeError("ai_invalid_response", "The AI model did not return a valid structured response.")

    def _call(
        self, prompt: str, schema: dict, *, model: str, timeout_seconds: float, temperature: float
    ) -> str:
        url = self._base_url.rstrip("/") + GENERATE_PATH
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {"temperature": temperature},
        }
        try:
            response = self._client.post(url, json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise DocPipeError("ai_timeout", "AI analysis timed out.") from exc
        except httpx.HTTPError as exc:
            raise DocPipeError("ai_unavailable", "AI analysis service is temporarily unavailable.") from exc

        if response.status_code != 200:
            raise DocPipeError("ai_processing_failed", "AI analysis failed.")

        try:
            body = response.json()
        except ValueError as exc:
            raise DocPipeError("ai_processing_failed", "AI analysis failed.") from exc

        return str(body.get("response", ""))

    @staticmethod
    def _parse_and_validate(raw: str, schema: dict) -> dict | None:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        try:
            jsonschema.validate(parsed, schema)
        except jsonschema.ValidationError:
            return None
        return parsed

    @staticmethod
    def _build_repair_prompt(original_prompt: str, invalid_output: str, schema: dict) -> str:
        return (
            "Your previous response was not valid JSON matching the required schema. "
            "Return ONLY a single valid JSON object that matches this JSON Schema "
            f"exactly, with no other text before or after it:\n\n{json.dumps(schema)}\n\n"
            f"Your previous (invalid) response was:\n{invalid_output}\n\n"
            f"Original task:\n{original_prompt}"
        )
