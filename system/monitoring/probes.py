"""Cheap, read-only status probes for Ollama and Stirling PDF.

Only GET requests to metadata endpoints - never a generation, a model
load/unload, or a PDF operation:

    Ollama:   GET /api/version, GET /api/tags, GET /api/ps
    Stirling: GET /api/v1/info/status   (public in Stirling v3.2.2; no
              API key is sent - it is not needed for this endpoint)

The target URLs come only from the existing server config
(`ollama.base_url`, `stirling.base_url`); redirects are not followed,
environment proxies are ignored, responses are size-capped and parsed
defensively. Model names and versions are kept only if they match a strict
pattern; everything else Ollama returns is ignored.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from system.monitoring.registry import safe_id, safe_version

MAX_RESPONSE_BYTES = 1024 * 1024
MAX_MODELS = 200
OLLAMA_PATHS = ("/api/version", "/api/tags", "/api/ps")
STIRLING_STATUS_PATH = "/api/v1/info/status"
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})?")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    service: str
    status: str
    http_status: int | None = None
    latency_ms: float | None = None
    version: str | None = None


@dataclass(frozen=True, slots=True)
class OllamaModel:
    name: str
    installed: bool
    loaded: bool
    size_bytes: int | None = None
    size_vram_bytes: int | None = None
    family: str | None = None
    parameter_size: str | None = None
    quantization: str | None = None
    context_length: int | None = None
    expires_ms: int | None = None


class _ProbeFailure(Exception):
    def __init__(self, status: str, http_status: int | None = None) -> None:
        super().__init__(status)
        self.status = status
        self.http_status = http_status


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _parse_expiry(value: object) -> int | None:
    """Go time.Time -> UTC epoch ms. The zero time (year 1) = no expiry."""
    if not isinstance(value, str):
        return None
    match = _ISO.fullmatch(value.strip())
    if not match:
        return None
    base, fraction, zone = match.groups()
    fraction = (fraction or ".0")[:7]
    zone = "+00:00" if zone in (None, "Z") else zone
    try:
        parsed = datetime.fromisoformat(f"{base}{fraction}{zone}")
    except ValueError:
        return None
    if parsed.year < 1971:
        return None
    return int(parsed.timestamp() * 1000)


class ServiceProber:
    def __init__(
        self,
        *,
        ollama_url: str,
        stirling_url: str,
        probe_ollama: bool,
        probe_stirling: bool,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._ollama_url = ollama_url.rstrip("/") if probe_ollama and ollama_url else ""
        self._stirling_url = stirling_url.rstrip("/") if probe_stirling and stirling_url else ""
        self._probe_ollama = probe_ollama
        self._probe_stirling = probe_stirling
        self._client = httpx.Client(
            follow_redirects=False, trust_env=False, timeout=timeout_seconds, transport=transport
        )

    def close(self) -> None:
        self._client.close()

    def _get_json(self, url: str) -> tuple[dict, int]:
        try:
            with self._client.stream("GET", url, headers={"Accept": "application/json"}) as response:
                if response.status_code != 200:
                    raise _ProbeFailure("http_error", response.status_code)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body += chunk
                    if len(body) > MAX_RESPONSE_BYTES:
                        raise _ProbeFailure("invalid_response", response.status_code)
        except httpx.TimeoutException as exc:
            raise _ProbeFailure("timeout") from exc
        except httpx.HTTPError as exc:
            raise _ProbeFailure("connection_error") from exc
        try:
            data = json.loads(bytes(body))
        except (ValueError, UnicodeDecodeError) as exc:
            raise _ProbeFailure("invalid_response", 200) from exc
        if not isinstance(data, dict):
            raise _ProbeFailure("invalid_response", 200)
        return data, 200

    def probe_stirling(self) -> ProbeResult:
        if not self._probe_stirling or not self._stirling_url:
            return ProbeResult("stirling", "not_configured")
        started = time.perf_counter()
        try:
            data, status = self._get_json(self._stirling_url + STIRLING_STATUS_PATH)
        except _ProbeFailure as failure:
            return ProbeResult(
                "stirling",
                failure.status,
                failure.http_status,
                round((time.perf_counter() - started) * 1000, 1),
            )
        latency = round((time.perf_counter() - started) * 1000, 1)
        if data.get("status") != "UP":
            return ProbeResult(
                "stirling", "invalid_response", status, latency, safe_version(data.get("version"))
            )
        return ProbeResult("stirling", "ok", status, latency, safe_version(data.get("version")))

    def probe_ollama(self) -> tuple[ProbeResult, list[OllamaModel]]:
        if not self._probe_ollama or not self._ollama_url:
            return ProbeResult("ollama", "not_configured"), []
        started = time.perf_counter()
        try:
            version_data, status = self._get_json(self._ollama_url + "/api/version")
            latency = round((time.perf_counter() - started) * 1000, 1)
            tags, _ = self._get_json(self._ollama_url + "/api/tags")
            running, _ = self._get_json(self._ollama_url + "/api/ps")
        except _ProbeFailure as failure:
            return ProbeResult(
                "ollama",
                failure.status,
                failure.http_status,
                round((time.perf_counter() - started) * 1000, 1),
            ), []
        return ProbeResult(
            "ollama", "ok", status, latency, safe_version(version_data.get("version"))
        ), self._models(tags, running)

    @staticmethod
    def _models(tags: dict, running: dict) -> list[OllamaModel]:
        def entries(data: dict) -> list[dict]:
            models = data.get("models")
            return [m for m in models[:MAX_MODELS] if isinstance(m, dict)] if isinstance(models, list) else []

        def details(entry: dict) -> dict:
            value = entry.get("details")
            return value if isinstance(value, dict) else {}

        loaded: dict[str, dict] = {}
        for entry in entries(running):
            name = safe_id(entry.get("name"))
            if name:
                loaded[name] = entry
        result: dict[str, OllamaModel] = {}
        for entry in entries(tags):
            name = safe_id(entry.get("name"))
            if not name:
                continue
            ps = loaded.get(name, {})
            d = details(entry)
            result[name] = OllamaModel(
                name=name,
                installed=True,
                loaded=name in loaded,
                size_bytes=_int(entry.get("size")),
                size_vram_bytes=_int(ps.get("size_vram")) if ps else None,
                family=safe_id(d.get("family")),
                parameter_size=safe_id(d.get("parameter_size")),
                quantization=safe_id(d.get("quantization_level")),
                context_length=_int(ps.get("context_length")) if ps else None,
                expires_ms=_parse_expiry(ps.get("expires_at")) if ps else None,
            )
        for name, ps in loaded.items():
            if name not in result:  # loaded but not listed (e.g. a race with a pull)
                d = details(ps)
                result[name] = OllamaModel(
                    name=name,
                    installed=False,
                    loaded=True,
                    size_bytes=_int(ps.get("size")),
                    size_vram_bytes=_int(ps.get("size_vram")),
                    family=safe_id(d.get("family")),
                    parameter_size=safe_id(d.get("parameter_size")),
                    quantization=safe_id(d.get("quantization_level")),
                    context_length=_int(ps.get("context_length")),
                    expires_ms=_parse_expiry(ps.get("expires_at")),
                )
        return list(result.values())[:MAX_MODELS]
