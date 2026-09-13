from __future__ import annotations

import pytest

from system.ai.modes import MODES
from system.ai.resolver import resolve_model_profile
from system.config import (
    ClientConfig,
    ClientServices,
    ModelProfile,
    OllamaSettings,
    ServerSettings,
    Settings,
    StirlingSettings,
)
from system.errors import DocPipeError

LIGHT = ModelProfile(provider="ollama", model="light-model", timeout_seconds=60, temperature=0)
STANDARD = ModelProfile(provider="ollama", model="standard-model", timeout_seconds=120, temperature=0)
HEAVY = ModelProfile(provider="ollama", model="heavy-model", timeout_seconds=180, temperature=0)


def _settings(models, clients=None) -> Settings:
    return Settings(
        server=ServerSettings(),
        stirling=StirlingSettings(base_url="http://stirling.test", api_key="", timeout_seconds=90),
        ollama=OllamaSettings(base_url="http://ollama.test"),
        models=models,
        clients=clients or {},
    )


def _client(model_overrides=None) -> ClientConfig:
    return ClientConfig(
        client_id="c1",
        enabled=True,
        api_key="k",
        services=ClientServices(documents=True, ai=True),
        model_overrides=model_overrides or {},
    )


def test_maintenance_extraction_defaults_to_light():
    settings = _settings(models={"light": LIGHT, "standard": STANDARD})
    profile_name, profile = resolve_model_profile(MODES["maintenance_extraction"], _client(), settings)
    assert profile_name == "light"
    assert profile is LIGHT


def test_inspection_extraction_defaults_to_standard():
    settings = _settings(models={"light": LIGHT, "standard": STANDARD})
    profile_name, profile = resolve_model_profile(MODES["inspection_extraction"], _client(), settings)
    assert profile_name == "standard"
    assert profile is STANDARD


def test_client_override_wins_over_mode_default():
    settings = _settings(models={"light": LIGHT, "standard": STANDARD})
    client = _client(model_overrides={"maintenance_extraction": "standard"})
    profile_name, profile = resolve_model_profile(MODES["maintenance_extraction"], client, settings)
    assert profile_name == "standard"
    assert profile is STANDARD


def test_client_override_for_other_mode_does_not_leak():
    settings = _settings(models={"light": LIGHT, "standard": STANDARD})
    client = _client(model_overrides={"maintenance_extraction": "standard"})
    # inspection_extraction has no override for this client - stays at its own default.
    profile_name, _ = resolve_model_profile(MODES["inspection_extraction"], client, settings)
    assert profile_name == "standard"  # inspection's own default, not maintenance's override


def test_unavailable_profile_raises_model_profile_unavailable_no_fallback():
    # "light" is mode's default but missing from `models` - must not
    # silently fall back to any other profile.
    settings = _settings(models={"standard": STANDARD, "heavy": HEAVY})
    with pytest.raises(DocPipeError) as excinfo:
        resolve_model_profile(MODES["maintenance_extraction"], _client(), settings)
    assert excinfo.value.code == "model_profile_unavailable"
    assert excinfo.value.status_code == 502


def test_client_override_to_unavailable_profile_raises_no_fallback_to_default():
    settings = _settings(models={"light": LIGHT})
    client = _client(model_overrides={"maintenance_extraction": "heavy"})
    with pytest.raises(DocPipeError) as excinfo:
        resolve_model_profile(MODES["maintenance_extraction"], client, settings)
    assert excinfo.value.code == "model_profile_unavailable"
