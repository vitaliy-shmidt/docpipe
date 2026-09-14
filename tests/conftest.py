from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from system import main as main_module
from system.config import (
    ClientConfig,
    ClientServices,
    ModelProfile,
    OllamaSettings,
    PromptSettings,
    ServerSettings,
    Settings,
    StirlingSettings,
)
from system.errors import DocPipeError

VALID_KEY = "valid-key"
NO_DOCS_KEY = "no-docs-key"
DISABLED_KEY = "disabled-key"
AI_KEY = "ai-key"
NO_AI_KEY = "no-ai-key"
AI_OVERRIDE_KEY = "ai-override-key"
# Prompt Lab (V2.2)
LAB_KEY = "lab-key"  # prompt_lab + ai - full Lab access, including /lab/analyze
LAB_NO_AI_KEY = "lab-no-ai-key"  # prompt_lab only, no ai - can list/load/save/activate but not /lab/analyze
# Assistant routing foundation (V2.2)
ASSISTANT_KEY = "assistant-key"

MAX_FILE_SIZE_MB = 1


def _raise_for_mode(mode: str) -> None:
    if mode == "unavailable":
        raise DocPipeError(
            "upstream_unavailable", "Document processing service is temporarily unavailable."
        )
    if mode == "timeout":
        raise DocPipeError("timeout", "Document processing timed out.")
    if mode == "auth_failed":
        raise DocPipeError("upstream_auth_failed", "Document processing service rejected the request.")
    if mode != "success":
        raise AssertionError(f"unexpected fake stirling mode: {mode}")


class FakeStirlingClient:
    """Test double standing in for the real Stirling HTTP client.

    Three independent knobs, one per upstream call the OCR fallback can
    make: `mode` for the first extract_text call, `ocr_mode` for ocr_pdf,
    `second_extract_mode` for the extract_text call run on the OCR'd PDF -
    independent so a test can make e.g. OCR itself succeed while the
    extraction that follows it fails.
    """

    def __init__(self, settings) -> None:
        self.settings = settings
        self.mode = "success"
        # Long enough to clear the default OCR threshold (30 meaningful
        # characters) on its own - this is the "normal, embedded-text PDF"
        # fixture value; short-text/scan scenarios set `self.text` per test.
        self.text = "This is extracted text from a normal text-based PDF document."
        self.ocr_mode = "success"
        self.second_extract_mode = "success"
        self.second_extract_text = "ocr extracted text"
        self.extract_calls = 0
        self.ocr_calls = 0

    def close(self) -> None:
        pass

    def extract_text(self, file_bytes: bytes, filename: str) -> str:
        self.extract_calls += 1
        if self.extract_calls == 1:
            _raise_for_mode(self.mode)
            return self.text
        _raise_for_mode(self.second_extract_mode)
        return self.second_extract_text

    def ocr_pdf(self, file_bytes: bytes, filename: str, languages) -> bytes:
        self.ocr_calls += 1
        _raise_for_mode(self.ocr_mode)
        return b"%PDF-1.4\nocr-output"


SAMPLE_MAINTENANCE_RESULT = {
    "vendor_name": "KONE GmbH",
    "service_type": "Aufzugswartung",
    "performed_at": "2026-09-12",
    "next_due_date": None,
    "technician": None,
    "result": "ohne Beanstandung",
    "cost": None,
    "currency": None,
    "notes": None,
}


class FakeOllamaClient:
    """Test double standing in for the real Ollama HTTP client."""

    def __init__(self, base_url: str, keep_alive: str | None = None) -> None:
        self.base_url = base_url
        self.keep_alive = keep_alive
        self.mode = "success"
        self.result = dict(SAMPLE_MAINTENANCE_RESULT)
        self.calls = 0
        self.last_prompt = None
        self.last_model = None
        self.last_timeout_seconds = None
        self.last_temperature = None
        # Ollama Keep-Alive/Timing pass: a route calls generate_structured()
        # with timing={} and reads the dict back afterward, identical to the
        # real OllamaClient - fake fixed values keep route-level tests
        # deterministic (no real wall-clock duration).
        self.fake_primary_duration_ms = 12.3
        self.fake_repair_duration_ms = 4.5
        # Warm-up: independent mode/counters from generate_structured's -
        # a test can make a normal query succeed while warm-up fails, or
        # vice versa, without the two paths interfering.
        self.warmup_mode = "success"
        self.warmup_calls = 0
        self.last_warmup_model = None
        self.last_warmup_timeout_seconds = None
        self.fake_warmup_duration_ms = 850.0

    def close(self) -> None:
        pass

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
        self.calls += 1
        self.last_prompt = prompt
        self.last_model = model
        self.last_timeout_seconds = timeout_seconds
        self.last_temperature = temperature
        if timing is not None:
            timing["ollama_primary_duration_ms"] = self.fake_primary_duration_ms
            repair_used = self.mode == "invalid_response"
            if repair_used:
                timing["ollama_repair_duration_ms"] = self.fake_repair_duration_ms
            timing["ollama_duration_ms"] = round(
                self.fake_primary_duration_ms + (self.fake_repair_duration_ms if repair_used else 0.0), 1
            )
        if self.mode == "success":
            return self.result
        if self.mode == "unavailable":
            raise DocPipeError("ai_unavailable", "AI analysis service is temporarily unavailable.")
        if self.mode == "timeout":
            raise DocPipeError("ai_timeout", "AI analysis timed out.")
        if self.mode == "invalid_response":
            raise DocPipeError(
                "ai_invalid_response", "The AI model did not return a valid structured response."
            )
        if self.mode == "processing_failed":
            raise DocPipeError("ai_processing_failed", "AI analysis failed.")
        raise AssertionError(f"unexpected fake ollama mode: {self.mode}")

    def warm_up(self, *, model: str, timeout_seconds: float, timing: dict | None = None) -> None:
        self.warmup_calls += 1
        self.last_warmup_model = model
        self.last_warmup_timeout_seconds = timeout_seconds
        if timing is not None:
            timing["ollama_duration_ms"] = self.fake_warmup_duration_ms
        if self.warmup_mode == "success":
            return
        if self.warmup_mode == "unavailable":
            raise DocPipeError("ai_unavailable", "AI analysis service is temporarily unavailable.")
        if self.warmup_mode == "timeout":
            raise DocPipeError("ai_timeout", "AI model warm-up timed out.")
        if self.warmup_mode == "processing_failed":
            raise DocPipeError("ai_processing_failed", "AI model warm-up failed.")
        raise AssertionError(f"unexpected fake ollama warmup mode: {self.warmup_mode}")


LIGHT_MODEL = "light-test-model"
STANDARD_MODEL = "standard-test-model"
HEAVY_MODEL = "heavy-test-model"

TEST_MODEL_PROFILES = {
    "light": ModelProfile(provider="ollama", model=LIGHT_MODEL, timeout_seconds=5, temperature=0),
    "standard": ModelProfile(provider="ollama", model=STANDARD_MODEL, timeout_seconds=5, temperature=0),
    "heavy": ModelProfile(provider="ollama", model=HEAVY_MODEL, timeout_seconds=5, temperature=0),
}


def _build_test_settings(runtime_prompts_dir: Path) -> Settings:
    return Settings(
        server=ServerSettings(max_file_size_mb=MAX_FILE_SIZE_MB),
        stirling=StirlingSettings(
            base_url="http://stirling.test", api_key="stirling-secret", timeout_seconds=5
        ),
        ollama=OllamaSettings(base_url="http://ollama.test"),
        # base_dir deliberately left at its default (the real
        # system/prompts/) - the repo's real v1.txt fixtures are exactly
        # what a test wants to see as "already there". Only runtime_dir is
        # swapped to a fresh per-test tmp_path so a Lab save/activate
        # during a test can never write into the real repository's
        # runtime-prompts/ directory.
        prompts=PromptSettings(runtime_dir=runtime_prompts_dir),
        models=dict(TEST_MODEL_PROFILES),
        clients={
            "enabled-client": ClientConfig(
                client_id="enabled-client",
                enabled=True,
                api_key=VALID_KEY,
                services=ClientServices(documents=True),
            ),
            "no-documents-client": ClientConfig(
                client_id="no-documents-client",
                enabled=True,
                api_key=NO_DOCS_KEY,
                services=ClientServices(documents=False),
            ),
            "disabled-client": ClientConfig(
                client_id="disabled-client",
                enabled=False,
                api_key=DISABLED_KEY,
                services=ClientServices(documents=True),
            ),
            "ai-client": ClientConfig(
                client_id="ai-client",
                enabled=True,
                api_key=AI_KEY,
                services=ClientServices(documents=True, ai=True),
            ),
            "no-ai-client": ClientConfig(
                client_id="no-ai-client",
                enabled=True,
                api_key=NO_AI_KEY,
                services=ClientServices(documents=True, ai=False),
            ),
            "ai-override-client": ClientConfig(
                client_id="ai-override-client",
                enabled=True,
                api_key=AI_OVERRIDE_KEY,
                services=ClientServices(documents=True, ai=True),
                # maintenance_extraction defaults to "light" - this client
                # is bumped to "standard" for that one mode only.
                model_overrides={"maintenance_extraction": "standard"},
            ),
            "lab-client": ClientConfig(
                client_id="lab-client",
                enabled=True,
                api_key=LAB_KEY,
                services=ClientServices(documents=True, ai=True, prompt_lab=True),
            ),
            "lab-no-ai-client": ClientConfig(
                client_id="lab-no-ai-client",
                enabled=True,
                api_key=LAB_NO_AI_KEY,
                services=ClientServices(documents=True, ai=False, prompt_lab=True),
            ),
            "assistant-client": ClientConfig(
                client_id="assistant-client",
                enabled=True,
                api_key=ASSISTANT_KEY,
                services=ClientServices(documents=True, ai=True, assistant=True),
            ),
        },
    )


@pytest.fixture
def fake_stirling_holder(monkeypatch, tmp_path):
    holder: dict = {}

    def factory(settings):
        instance = FakeStirlingClient(settings)
        holder["client"] = instance
        return instance

    monkeypatch.setattr(main_module, "StirlingClient", factory)
    monkeypatch.setattr(
        main_module, "load_settings", lambda: _build_test_settings(tmp_path / "runtime-prompts")
    )
    return holder


@pytest.fixture
def fake_ollama_holder(monkeypatch):
    holder: dict = {}

    def factory(base_url, keep_alive=None):
        instance = FakeOllamaClient(base_url, keep_alive=keep_alive)
        holder["client"] = instance
        return instance

    monkeypatch.setattr(main_module, "OllamaClient", factory)
    return holder


@pytest.fixture
def client(fake_stirling_holder, fake_ollama_holder):
    with TestClient(main_module.app) as test_client:
        test_client.fake_stirling = fake_stirling_holder
        test_client.fake_ollama = fake_ollama_holder
        yield test_client


def auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def pdf_bytes(total_size: int = 2048) -> bytes:
    header = b"%PDF-1.4\n"
    padding = b"0" * max(0, total_size - len(header))
    return header + padding
