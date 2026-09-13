from __future__ import annotations

from system.config import load_settings


def test_v1_style_config_without_ollama_or_ai_still_loads(tmp_path, monkeypatch):
    """A pre-V2 config.yaml (no [ollama] section, no services.ai key) must
    keep working unchanged: ai defaults to disabled, ollama settings default
    to empty/harmless values instead of raising.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
server:
  max_file_size_mb: 25

stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

clients:
  legacy-client:
    enabled: true
    api_key: "legacy-key"
    services:
      documents: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    for var in ("OLLAMA_URL", "OLLAMA_MODEL", "OLLAMA_TIMEOUT"):
        monkeypatch.delenv(var, raising=False)

    settings = load_settings()

    client = settings.clients["legacy-client"]
    assert client.services.documents is True
    assert client.services.ai is False
    assert settings.ollama.base_url == ""
    assert settings.ollama.model == ""
    assert settings.ollama.timeout_seconds == 120
    assert settings.ollama.temperature == 0.0


def test_ollama_env_overrides_take_priority_over_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

ollama:
  base_url: "http://from-file:11434"
  model: "from-file-model"
  timeout_seconds: 60
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.setenv("OLLAMA_URL", "http://from-env:11434")
    monkeypatch.setenv("OLLAMA_MODEL", "from-env-model")
    monkeypatch.setenv("OLLAMA_TIMEOUT", "30")

    settings = load_settings()

    assert settings.ollama.base_url == "http://from-env:11434"
    assert settings.ollama.model == "from-env-model"
    assert settings.ollama.timeout_seconds == 30
