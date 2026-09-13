"""DocPipe server configuration.

Configuration is loaded once at process startup from a local YAML file.
There is no database and no runtime reload.

Load order / priority (highest wins):
  1. Environment variables (STIRLING_URL, STIRLING_API_KEY, STIRLING_TIMEOUT,
     OLLAMA_URL)
  2. Values from the YAML config file
  3. Built-in defaults

The path to the YAML config file itself is controlled by the
DOCPIPE_CONFIG environment variable and defaults to "config.yaml"
in the current working directory.

AI model selection (V2.1): the client picks a *mode* (a task); DocPipe -
never the client - picks the *model*, via a named model profile (e.g.
"light"/"standard"/"heavy"). See system/ai/modes.py (mode -> default
profile name) and system/ai/resolver.py (profile name -> ModelProfile,
with optional per-client override). This file only defines what a
profile *is* and validates that the routing is internally consistent.

Breaking change from the original V2 pass: `ollama.model`/
`ollama.timeout_seconds`/`ollama.temperature` no longer exist - a single
global model doesn't fit "the client selects a task, DocPipe selects the
model" once more than one mode/model class exists. `ollama:` now only
holds the provider-wide `base_url`; model/timeout/temperature come from
the resolved model profile instead. This project has not been deployed
yet, so this config shape is changed directly rather than carrying a
compatibility shim for a format that was never used in production.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from system.ai.modes import MODES

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_MAX_FILE_SIZE_MB = 25
DEFAULT_STIRLING_TIMEOUT_SECONDS = 90

SUPPORTED_MODEL_PROVIDERS = {"ollama"}


class ConfigError(Exception):
    """Raised for an internally inconsistent config - always at startup, never per-request."""


@dataclass(frozen=True)
class StirlingSettings:
    base_url: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class OllamaSettings:
    """Provider-wide Ollama connection settings. No model lives here."""

    base_url: str


@dataclass(frozen=True)
class ModelProfile:
    """A named resource/quality class (e.g. "light"), not a specific vendor.

    `provider` is kept as a real field - only "ollama" is implemented in
    V2.1 - so a second provider can be added later without reshaping this
    dataclass or the config file format.
    """

    provider: str
    model: str
    timeout_seconds: int
    temperature: float = 0.0


@dataclass(frozen=True)
class ClientServices:
    documents: bool = False
    # Defaults to False when a client config predates V2 (or simply omits
    # the key) - AI access is always an explicit opt-in, never inherited.
    ai: bool = False


@dataclass(frozen=True)
class ClientConfig:
    client_id: str
    enabled: bool
    api_key: str
    services: ClientServices
    # mode name -> model profile name (never a raw model name - see
    # _validate_client_override_type below). Empty for most clients.
    model_overrides: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ServerSettings:
    max_file_size_mb: int = DEFAULT_MAX_FILE_SIZE_MB

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    server: ServerSettings
    stirling: StirlingSettings
    ollama: OllamaSettings
    models: dict[str, ModelProfile] = field(default_factory=dict)
    clients: dict[str, ClientConfig] = field(default_factory=dict)

    def find_client_by_api_key(self, api_key: str) -> ClientConfig | None:
        for client in self.clients.values():
            if client.api_key == api_key:
                return client
        return None


def _load_yaml(path: str) -> dict:
    if not os.path.isfile(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _parse_model_profiles(raw_models: object) -> dict[str, ModelProfile]:
    if not raw_models:
        return {}
    if not isinstance(raw_models, dict):
        raise ConfigError("'models' must be a mapping of profile name -> profile definition.")

    profiles: dict[str, ModelProfile] = {}
    for profile_name, raw_profile in raw_models.items():
        if not isinstance(raw_profile, dict):
            raise ConfigError(f"Model profile '{profile_name}' must be a mapping.")
        try:
            timeout_seconds = int(raw_profile.get("timeout_seconds", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Model profile '{profile_name}' has a non-numeric timeout_seconds.") from exc
        try:
            temperature = float(raw_profile.get("temperature", 0.0))
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Model profile '{profile_name}' has a non-numeric temperature.") from exc

        profiles[profile_name] = ModelProfile(
            provider=str(raw_profile.get("provider", "")),
            model=str(raw_profile.get("model", "")),
            timeout_seconds=timeout_seconds,
            temperature=temperature,
        )
    return profiles


def _parse_model_overrides(client_id: str, raw_overrides: object) -> dict[str, str]:
    if not raw_overrides:
        return {}
    if not isinstance(raw_overrides, dict):
        raise ConfigError(f"Client '{client_id}' has a non-mapping model_overrides.")

    overrides: dict[str, str] = {}
    for mode_name, profile_name in raw_overrides.items():
        if not isinstance(profile_name, str):
            raise ConfigError(
                f"Client '{client_id}' has a non-string model_overrides value for mode '{mode_name}'."
            )
        overrides[mode_name] = profile_name
    return overrides


def _validate_model_profile_shapes(models: dict[str, ModelProfile]) -> None:
    for profile_name, profile in models.items():
        if not profile.provider:
            raise ConfigError(f"Model profile '{profile_name}' is missing 'provider'.")
        if profile.provider not in SUPPORTED_MODEL_PROVIDERS:
            raise ConfigError(
                f"Model profile '{profile_name}' has unsupported provider '{profile.provider}' "
                f"(supported: {sorted(SUPPORTED_MODEL_PROVIDERS)})."
            )
        if not profile.model:
            raise ConfigError(f"Model profile '{profile_name}' is missing 'model'.")
        if profile.timeout_seconds <= 0:
            raise ConfigError(
                f"Model profile '{profile_name}' has an invalid timeout_seconds: {profile.timeout_seconds} "
                "(must be > 0)."
            )
        if profile.temperature < 0:
            raise ConfigError(
                f"Model profile '{profile_name}' has an invalid temperature: {profile.temperature} "
                "(must be >= 0)."
            )


def _validate_mode_routing(models: dict[str, ModelProfile]) -> None:
    for mode_name, mode in MODES.items():
        if mode.model_profile not in models:
            raise ConfigError(
                f"Mode '{mode_name}' defaults to model profile '{mode.model_profile}', "
                "which is not defined under 'models'."
            )


def _validate_client_overrides(clients: dict[str, ClientConfig], models: dict[str, ModelProfile]) -> None:
    for client in clients.values():
        for mode_name, profile_name in client.model_overrides.items():
            if mode_name not in MODES:
                raise ConfigError(
                    f"Client '{client.client_id}' has a model_overrides entry for unknown mode '{mode_name}'."
                )
            if not profile_name or profile_name not in models:
                raise ConfigError(
                    f"Client '{client.client_id}' overrides mode '{mode_name}' to unknown model "
                    f"profile '{profile_name!r}'."
                )


def _is_ai_configured(raw_models: object, clients: dict[str, ClientConfig]) -> bool:
    if raw_models:
        return True
    return any(client.services.ai or client.model_overrides for client in clients.values())


def validate_ai_config(settings: Settings) -> None:
    """Fail-fast validation of model profiles and mode/client routing.

    Raises ConfigError on the first inconsistency found. Intended to be
    called once at startup (see load_settings()) - a broken AI config
    must prevent the process from starting, not surface as a per-request
    500 the first time a client happens to call /analyze.
    """
    _validate_model_profile_shapes(settings.models)
    _validate_mode_routing(settings.models)
    _validate_client_overrides(settings.clients, settings.models)


def load_settings() -> Settings:
    config_path = os.environ.get("DOCPIPE_CONFIG", DEFAULT_CONFIG_PATH)
    raw = _load_yaml(config_path)

    raw_server = raw.get("server") or {}
    server = ServerSettings(
        max_file_size_mb=int(raw_server.get("max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB)),
    )

    raw_stirling = raw.get("stirling") or {}
    stirling = StirlingSettings(
        base_url=os.environ.get("STIRLING_URL", raw_stirling.get("base_url", "")),
        api_key=os.environ.get("STIRLING_API_KEY", raw_stirling.get("api_key", "")),
        timeout_seconds=int(
            os.environ.get(
                "STIRLING_TIMEOUT",
                raw_stirling.get("timeout_seconds", DEFAULT_STIRLING_TIMEOUT_SECONDS),
            )
        ),
    )

    raw_ollama = raw.get("ollama") or {}
    ollama = OllamaSettings(base_url=os.environ.get("OLLAMA_URL", raw_ollama.get("base_url", "")))

    raw_models = raw.get("models")
    models = _parse_model_profiles(raw_models)

    clients: dict[str, ClientConfig] = {}
    for client_id, raw_client in (raw.get("clients") or {}).items():
        raw_services = raw_client.get("services") or {}
        clients[client_id] = ClientConfig(
            client_id=client_id,
            enabled=bool(raw_client.get("enabled", False)),
            api_key=str(raw_client.get("api_key", "")),
            services=ClientServices(
                documents=bool(raw_services.get("documents", False)),
                ai=bool(raw_services.get("ai", False)),
            ),
            model_overrides=_parse_model_overrides(client_id, raw_client.get("model_overrides")),
        )

    settings = Settings(server=server, stirling=stirling, ollama=ollama, models=models, clients=clients)

    if _is_ai_configured(raw_models, clients):
        validate_ai_config(settings)

    return settings
