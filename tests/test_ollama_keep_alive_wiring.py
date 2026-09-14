"""Ollama Keep-Alive tuning pass (task §8/§31): confirms the wiring from
Settings.ollama.keep_alive -> system/main.py's lifespan -> the single
OllamaClient instance shared by /documents/analyze and /assistant/query -
not just that OllamaClient itself accepts/sends the value (see
test_ollama_client.py for that unit-level coverage)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from system import main as main_module
from tests.conftest import _build_test_settings


def test_configured_keep_alive_reaches_the_shared_ollama_client(monkeypatch, tmp_path):
    base_settings = _build_test_settings(tmp_path / "runtime-prompts")
    custom_settings = base_settings.__class__(
        server=base_settings.server,
        stirling=base_settings.stirling,
        ollama=base_settings.ollama.__class__(base_url="http://ollama.test", keep_alive="7m"),
        prompts=base_settings.prompts,
        models=base_settings.models,
        clients=base_settings.clients,
    )
    monkeypatch.setattr(main_module, "load_settings", lambda: custom_settings)
    monkeypatch.setattr(main_module, "StirlingClient", lambda settings: _NoopStirlingClient())

    captured: dict = {}

    def factory(base_url, keep_alive=None):
        captured["base_url"] = base_url
        captured["keep_alive"] = keep_alive
        return _NoopOllamaClient()

    monkeypatch.setattr(main_module, "OllamaClient", factory)

    with TestClient(main_module.app):
        pass

    assert captured["keep_alive"] == "7m"


class _NoopStirlingClient:
    def close(self) -> None:
        pass


class _NoopOllamaClient:
    def close(self) -> None:
        pass
