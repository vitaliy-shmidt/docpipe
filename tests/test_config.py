from __future__ import annotations

import pytest

from system.config import (
    ClientConfig,
    ClientServices,
    ConfigError,
    ModelProfile,
    OllamaSettings,
    ServerSettings,
    Settings,
    StirlingSettings,
    load_settings,
    validate_ai_config,
)

VALID_PROFILES = {
    "light": ModelProfile(provider="ollama", model="light-model", timeout_seconds=60, temperature=0),
    "standard": ModelProfile(provider="ollama", model="standard-model", timeout_seconds=120, temperature=0),
    "heavy": ModelProfile(provider="ollama", model="heavy-model", timeout_seconds=180, temperature=0),
}


def _settings(models=None, clients=None) -> Settings:
    return Settings(
        server=ServerSettings(),
        stirling=StirlingSettings(base_url="http://stirling.test", api_key="", timeout_seconds=90),
        ollama=OllamaSettings(base_url="http://ollama.test"),
        models=models if models is not None else {},
        clients=clients if clients is not None else {},
    )


# --- V1/V2 backward compatibility -------------------------------------------


def test_v1_style_config_without_ollama_or_ai_still_loads(tmp_path, monkeypatch):
    """A pre-V2.1 config.yaml (no [ollama]/[models] section, no services.ai
    key) must keep working unchanged: ai defaults to disabled, no model
    profiles are required, nothing raises.
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
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    settings = load_settings()

    client = settings.clients["legacy-client"]
    assert client.services.documents is True
    assert client.services.ai is False
    assert client.model_overrides == {}
    assert settings.ollama.base_url == ""
    assert settings.models == {}


def test_config_without_ocr_block_uses_enabled_defaults(tmp_path, monkeypatch):
    """A config predating the OCR fallback (no `stirling.ocr:` key) must
    still load, with OCR on by default - see config.py's DEFAULT_OCR_*
    constants for why enabled defaults to True rather than False."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    settings = load_settings()

    assert settings.stirling.ocr.enabled is True
    assert settings.stirling.ocr.languages == ("deu", "eng")
    assert settings.stirling.ocr.min_meaningful_characters == 30
    assert settings.stirling.ocr.timeout_seconds == 180


def test_config_with_explicit_ocr_block_overrides_defaults(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90
  ocr:
    enabled: false
    languages:
      - eng
    min_meaningful_characters: 50
    timeout_seconds: 240
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    settings = load_settings()

    assert settings.stirling.ocr.enabled is False
    assert settings.stirling.ocr.languages == ("eng",)
    assert settings.stirling.ocr.min_meaningful_characters == 50
    assert settings.stirling.ocr.timeout_seconds == 240


def test_ollama_url_env_override_takes_priority_over_file(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

ollama:
  base_url: "http://from-file:11434"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.setenv("OLLAMA_URL", "http://from-env:11434")

    settings = load_settings()

    assert settings.ollama.base_url == "http://from-env:11434"


# --- Full V2.1 profile config, loaded from YAML -----------------------------


def test_full_v2_1_config_with_profiles_and_overrides_loads(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

ollama:
  base_url: "http://ollama:11434"

models:
  light:
    provider: ollama
    model: "light-model"
    timeout_seconds: 60
    temperature: 0
  standard:
    provider: ollama
    model: "standard-model"
    timeout_seconds: 120
    temperature: 0
  heavy:
    provider: ollama
    model: "heavy-model"
    timeout_seconds: 180
    temperature: 0

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
      ai: true
    model_overrides:
      maintenance_extraction: standard
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    settings = load_settings()

    assert set(settings.models.keys()) == {"light", "standard", "heavy"}
    assert settings.models["light"].model == "light-model"
    assert settings.clients["demo-client"].model_overrides == {"maintenance_extraction": "standard"}


def test_client_with_ai_enabled_but_no_models_section_fails_fast(tmp_path, monkeypatch):
    """A client opts into ai but the operator forgot `models:` entirely -
    this must crash at startup (load_settings), not on the first request.
    """
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
      ai: true
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    with pytest.raises(ConfigError):
        load_settings()


def test_non_string_model_override_value_fails_fast(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

models:
  light:
    provider: ollama
    model: "light-model"
    timeout_seconds: 60

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
      ai: true
    model_overrides:
      maintenance_extraction: 123
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCPIPE_CONFIG", str(config_path))
    monkeypatch.delenv("OLLAMA_URL", raising=False)

    with pytest.raises(ConfigError):
        load_settings()


# --- validate_ai_config() unit tests -----------------------------------------


def test_valid_light_standard_heavy_profiles_pass_validation():
    validate_ai_config(_settings(models=VALID_PROFILES))  # must not raise


def test_missing_model_field_fails_validation():
    profiles = {**VALID_PROFILES, "light": ModelProfile(provider="ollama", model="", timeout_seconds=60)}
    with pytest.raises(ConfigError, match="model"):
        validate_ai_config(_settings(models=profiles))


def test_invalid_timeout_fails_validation():
    profiles = {
        **VALID_PROFILES,
        "light": ModelProfile(provider="ollama", model="x", timeout_seconds=0),
    }
    with pytest.raises(ConfigError, match="timeout_seconds"):
        validate_ai_config(_settings(models=profiles))


def test_negative_timeout_fails_validation():
    profiles = {
        **VALID_PROFILES,
        "light": ModelProfile(provider="ollama", model="x", timeout_seconds=-5),
    }
    with pytest.raises(ConfigError, match="timeout_seconds"):
        validate_ai_config(_settings(models=profiles))


def test_unsupported_provider_fails_validation():
    profiles = {**VALID_PROFILES, "light": ModelProfile(provider="openai", model="x", timeout_seconds=60)}
    with pytest.raises(ConfigError, match="provider"):
        validate_ai_config(_settings(models=profiles))


def test_negative_temperature_fails_validation():
    profiles = {
        **VALID_PROFILES,
        "light": ModelProfile(provider="ollama", model="x", timeout_seconds=60, temperature=-1),
    }
    with pytest.raises(ConfigError, match="temperature"):
        validate_ai_config(_settings(models=profiles))


def test_mode_default_profile_missing_fails_validation():
    # maintenance_extraction defaults to "light" (see system/ai/modes.py) -
    # dropping it from `models` must be caught even with standard/heavy present.
    profiles = {k: v for k, v in VALID_PROFILES.items() if k != "light"}
    with pytest.raises(ConfigError, match="light"):
        validate_ai_config(_settings(models=profiles))


def test_unknown_client_override_profile_fails_validation():
    client = ClientConfig(
        client_id="c1",
        enabled=True,
        api_key="k",
        services=ClientServices(documents=True, ai=True),
        model_overrides={"maintenance_extraction": "does-not-exist"},
    )
    with pytest.raises(ConfigError, match="does-not-exist"):
        validate_ai_config(_settings(models=VALID_PROFILES, clients={"c1": client}))


def test_unknown_client_override_mode_fails_validation():
    client = ClientConfig(
        client_id="c1",
        enabled=True,
        api_key="k",
        services=ClientServices(documents=True, ai=True),
        model_overrides={"not_a_real_mode": "light"},
    )
    with pytest.raises(ConfigError, match="not_a_real_mode"):
        validate_ai_config(_settings(models=VALID_PROFILES, clients={"c1": client}))
