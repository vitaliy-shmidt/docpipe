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
         "options": {"temperature": ...}, "keep_alive": "15m"}
  Response: {"response": "<generated text, JSON-serialized>",
             "total_duration": <ns>, "load_duration": <ns>,
             "prompt_eval_duration": <ns>, "eval_duration": <ns>,
             "eval_count": <int>, ...}

Passing the mode's response_schema as `format` (rather than the literal
string "json") constrains Ollama's own grammar-based decoding to that
exact schema - the strongest available guarantee before DocPipe's own
schema validation runs as a second, independent check.

This client only knows the provider-wide `base_url` and `keep_alive`
(system/config.py's OllamaSettings, resolved once at startup - see
system/main.py). Which model to run, with what timeout and temperature, is
resolved elsewhere (system/ai/resolver.py, from the request's mode and the
calling client's config) and handed in per call - the client itself has no
notion of modes, profiles, or client overrides, and `keep_alive` is never
settable per-request (task §6: infrastructure config, not a client input -
AnalyzeRequest/AssistantQueryRequest both use `extra="forbid"`, so a client
attempting to send it gets a plain invalid_request, not a silent override).

Ollama Keep-Alive tuning pass (see docs/staging-deployment.md "Ollama
Keep-Alive"): `keep_alive` is sent identically on every call this client
makes - the single OllamaClient instance is shared by both
/documents/analyze and /assistant/query (system/main.py), so there is no
route that can drift out of sync with another (task §8 Single Source of
Truth).
"""

from __future__ import annotations

import json
import time

import httpx
import jsonschema

from system.errors import DocPipeError

GENERATE_PATH = "/api/generate"

# Task §16: Ollama's own response timing fields, all in nanoseconds - never
# required (task §17: an older Ollama version that omits them must not fail
# the request), read defensively and converted to milliseconds only when
# present and numeric.
_OLLAMA_NS_METRIC_FIELDS = {
    "load_duration": "load_ms",
    "prompt_eval_duration": "prompt_eval_ms",
    "eval_duration": "eval_ms",
}


class OllamaClient:
    def __init__(self, base_url: str, keep_alive: str | None = None) -> None:
        self._base_url = base_url
        self._keep_alive = keep_alive
        self._client = httpx.Client(follow_redirects=False)

    def close(self) -> None:
        self._client.close()

    def generate_structured(
        self,
        prompt: str,
        schema: dict,
        *,
        model: str,
        timeout_seconds: float,
        temperature: float,
        timing: dict | None = None,
    ) -> dict:
        """Runs prompt against `model` and validates the JSON result against schema.

        Makes exactly one repair attempt if the first response is not valid
        JSON or does not match schema. Raises DocPipeError("ai_invalid_response")
        if it is still invalid after that single retry - never loops further.

        `timing`, if given, is populated in place (task §12/§13) with
        `ollama_primary_duration_ms`, `ollama_repair_duration_ms` (only when
        a repair call actually happened) and the summed
        `ollama_duration_ms` - plus, when Ollama's own response included
        them, `ollama_primary_load_ms`/`_prompt_eval_ms`/`_eval_ms`/
        `_eval_count` (and the `_repair_` equivalents). This is an output
        parameter rather than a return-value change so the method's return
        contract (a plain result dict) stays exactly as every existing
        caller/test already expects.
        """
        raw, metrics = self._call(
            prompt, schema, model=model, timeout_seconds=timeout_seconds, temperature=temperature
        )
        self._record_timing(timing, "primary", metrics)
        result = self._parse_and_validate(raw, schema)
        if result is not None:
            self._finalize_timing(timing)
            return result

        repair_prompt = self._build_repair_prompt(prompt, raw, schema)
        raw, metrics = self._call(
            repair_prompt, schema, model=model, timeout_seconds=timeout_seconds, temperature=temperature
        )
        self._record_timing(timing, "repair", metrics)
        result = self._parse_and_validate(raw, schema)
        if result is not None:
            self._finalize_timing(timing)
            return result

        self._finalize_timing(timing)
        raise DocPipeError("ai_invalid_response", "The AI model did not return a valid structured response.")

    def _call(
        self, prompt: str, schema: dict, *, model: str, timeout_seconds: float, temperature: float
    ) -> tuple[str, dict]:
        url = self._base_url.rstrip("/") + GENERATE_PATH
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "format": schema,
            "options": {"temperature": temperature},
        }
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        started_at = time.monotonic()
        try:
            response = self._client.post(url, json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise DocPipeError("ai_timeout", "AI analysis timed out.") from exc
        except httpx.HTTPError as exc:
            raise DocPipeError("ai_unavailable", "AI analysis service is temporarily unavailable.") from exc
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)

        if response.status_code != 200:
            raise DocPipeError("ai_processing_failed", "AI analysis failed.")

        try:
            body = response.json()
        except ValueError as exc:
            raise DocPipeError("ai_processing_failed", "AI analysis failed.") from exc

        metrics = {"duration_ms": duration_ms, **self._extract_ollama_metrics(body)}
        return str(body.get("response", "")), metrics

    def warm_up(self, *, model: str, timeout_seconds: float, timing: dict | None = None) -> None:
        """Load `model` into Ollama's memory without producing a real answer.

        Same request/error-mapping shape as `_call`, but with no `format`
        schema (a warm-up has no structured answer to validate) and no
        repair loop - the caller only cares that Ollama accepted the model
        and, if configured, refreshed `keep_alive`.
        """
        url = self._base_url.rstrip("/") + GENERATE_PATH
        payload = {"model": model, "prompt": "", "stream": False}
        if self._keep_alive:
            payload["keep_alive"] = self._keep_alive
        started_at = time.monotonic()
        try:
            response = self._client.post(url, json=payload, timeout=timeout_seconds)
        except httpx.TimeoutException as exc:
            raise DocPipeError("ai_timeout", "AI model warm-up timed out.") from exc
        except httpx.HTTPError as exc:
            raise DocPipeError("ai_unavailable", "AI analysis service is temporarily unavailable.") from exc
        duration_ms = round((time.monotonic() - started_at) * 1000, 1)

        if response.status_code != 200:
            raise DocPipeError("ai_processing_failed", "AI model warm-up failed.")

        try:
            body = response.json()
        except ValueError as exc:
            raise DocPipeError("ai_processing_failed", "AI model warm-up failed.") from exc

        if timing is not None:
            timing["ollama_duration_ms"] = duration_ms
            for key, value in self._extract_ollama_metrics(body).items():
                timing[f"ollama_{key}"] = value

    @staticmethod
    def _extract_ollama_metrics(body: dict) -> dict:
        metrics: dict = {}
        for src_field, dest_key in _OLLAMA_NS_METRIC_FIELDS.items():
            value = body.get(src_field)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                metrics[dest_key] = round(value / 1_000_000, 1)
        eval_count = body.get("eval_count")
        if isinstance(eval_count, int) and not isinstance(eval_count, bool):
            metrics["eval_count"] = eval_count
        return metrics

    @staticmethod
    def _record_timing(timing: dict | None, phase: str, metrics: dict) -> None:
        if timing is None:
            return
        timing[f"ollama_{phase}_duration_ms"] = metrics["duration_ms"]
        for key in ("load_ms", "prompt_eval_ms", "eval_ms", "eval_count"):
            if key in metrics:
                timing[f"ollama_{phase}_{key}"] = metrics[key]

    @staticmethod
    def _finalize_timing(timing: dict | None) -> None:
        if timing is None:
            return
        primary = timing.get("ollama_primary_duration_ms") or 0.0
        repair = timing.get("ollama_repair_duration_ms") or 0.0
        timing["ollama_duration_ms"] = round(primary + repair, 1)

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
