from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from system import main as main_module
from system.config import (
    ClientConfig,
    ClientServices,
    OllamaSettings,
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

MAX_FILE_SIZE_MB = 1


class FakeStirlingClient:
    """Test double standing in for the real Stirling HTTP client."""

    def __init__(self, settings) -> None:
        self.settings = settings
        self.mode = "success"
        self.text = "extracted text"
        self.calls = 0

    def close(self) -> None:
        pass

    def extract_text(self, file_bytes: bytes, filename: str) -> str:
        self.calls += 1
        if self.mode == "success":
            return self.text
        if self.mode == "unavailable":
            raise DocPipeError(
                "upstream_unavailable", "Document processing service is temporarily unavailable."
            )
        if self.mode == "timeout":
            raise DocPipeError("timeout", "Document processing timed out.")
        if self.mode == "auth_failed":
            raise DocPipeError("upstream_auth_failed", "Document processing service rejected the request.")
        raise AssertionError(f"unexpected fake stirling mode: {self.mode}")


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

    def __init__(self, settings) -> None:
        self.settings = settings
        self.mode = "success"
        self.result = dict(SAMPLE_MAINTENANCE_RESULT)
        self.calls = 0
        self.last_prompt = None

    def close(self) -> None:
        pass

    def generate_structured(self, prompt: str, schema: dict) -> dict:
        self.calls += 1
        self.last_prompt = prompt
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


def _build_test_settings() -> Settings:
    return Settings(
        server=ServerSettings(max_file_size_mb=MAX_FILE_SIZE_MB),
        stirling=StirlingSettings(
            base_url="http://stirling.test", api_key="stirling-secret", timeout_seconds=5
        ),
        ollama=OllamaSettings(
            base_url="http://ollama.test", model="test-model", timeout_seconds=5, temperature=0
        ),
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
        },
    )


@pytest.fixture
def fake_stirling_holder(monkeypatch):
    holder: dict = {}

    def factory(settings):
        instance = FakeStirlingClient(settings)
        holder["client"] = instance
        return instance

    monkeypatch.setattr(main_module, "StirlingClient", factory)
    monkeypatch.setattr(main_module, "load_settings", _build_test_settings)
    return holder


@pytest.fixture
def fake_ollama_holder(monkeypatch):
    holder: dict = {}

    def factory(settings):
        instance = FakeOllamaClient(settings)
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
