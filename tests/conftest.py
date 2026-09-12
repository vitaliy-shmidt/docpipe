from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from system import main as main_module
from system.config import ClientConfig, ClientServices, ServerSettings, Settings, StirlingSettings
from system.errors import DocPipeError

VALID_KEY = "valid-key"
NO_DOCS_KEY = "no-docs-key"
DISABLED_KEY = "disabled-key"

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


def _build_test_settings() -> Settings:
    return Settings(
        server=ServerSettings(max_file_size_mb=MAX_FILE_SIZE_MB),
        stirling=StirlingSettings(
            base_url="http://stirling.test", api_key="stirling-secret", timeout_seconds=5
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
def client(fake_stirling_holder):
    with TestClient(main_module.app) as test_client:
        test_client.fake_stirling = fake_stirling_holder
        yield test_client


def auth_headers(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


def pdf_bytes(total_size: int = 2048) -> bytes:
    header = b"%PDF-1.4\n"
    padding = b"0" * max(0, total_size - len(header))
    return header + padding
