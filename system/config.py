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
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.prompt_registry import PromptRegistry
from system.assistant.modes import ASSISTANT_MODES

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_MAX_FILE_SIZE_MB = 25
DEFAULT_STIRLING_TIMEOUT_SECONDS = 90

# Ollama Keep-Alive tuning pass (see docs/staging-deployment.md "Ollama
# Keep-Alive"). Real staging observation: the deployed qwen2.5:1.5b-instruct
# model unloads from Ollama ~4 minutes after last use (Ollama's own default),
# forcing a cold reload (and a request that can exceed HubDix's HTTP
# timeout) on the next call. A conservative *code* default of "5m" is used
# here deliberately - `config.example.yaml` documents the recommended "15m"
# production override, but a config that omits `ollama.keep_alive` entirely
# (e.g. an existing deployment that hasn't been touched yet) should not
# silently jump to the longer, more RAM-hungry value (see task §4/§48 "kein
# Forever", 8 GB RAM budget).
DEFAULT_OLLAMA_KEEP_ALIVE = "5m"
# Task §5: a positive Go-style duration ("30s"/"5m"/"15m"/"1h") or a plain
# positive integer (seconds, also accepted by Ollama's API). Deliberately
# excludes 0 and negative values (Ollama-specific meanings: "unload
# immediately" / "keep forever") - neither is a documented, intentional
# choice for this deployment (task §48 "kein Forever"), so a config author
# who wants either must not be able to reach it through a typo.
OLLAMA_KEEP_ALIVE_PATTERN = re.compile(r"^[1-9][0-9]*(s|m|h)?$")

# See system/ai/prompt_registry.py. base_dir defaults to the exact same
# directory system/ai/modes.py has always loaded schemas from - no second
# hardcoded path to drift out of sync with it. runtime_dir defaults to a
# plain relative path (like DEFAULT_CONFIG_PATH) - in production this is a
# mounted, persistent volume (see docker-compose.yml), in a local/dev run
# it's simply created on first Prompt Lab save.
DEFAULT_PROMPTS_BASE_DIR = PROMPTS_DIR
DEFAULT_PROMPTS_RUNTIME_DIR = Path("runtime-prompts")
# Generous but bounded (see task §55) - a real prompt is a few KB; this is
# a safety net against an accidental huge paste, not a tuned limit.
DEFAULT_PROMPT_MAX_CONTENT_LENGTH = 32_000

# OCR is a fallback for scanned/image-only PDFs, used only when plain text
# extraction comes back with too little real text - see
# system/text_quality.py. Deliberately enabled by default: the production
# Stirling instance this targets already ships Tesseract German/English
# language data, so a client pointing at a fresh Stirling without OCR tools
# installed will simply see OCR attempts fail with the existing
# upstream_unavailable/processing_failed codes - no new failure mode is
# introduced by defaulting to on.
DEFAULT_OCR_ENABLED = True
DEFAULT_OCR_LANGUAGES = ("deu", "eng")
DEFAULT_OCR_MIN_MEANINGFUL_CHARACTERS = 30
# OCR (rendering every page to an image and running Tesseract/OCRmyPDF) is
# far slower than plain text extraction - a separate, longer timeout so a
# realistic multi-page scan doesn't get cut off by the fast-path's
# stirling.timeout_seconds, without making that fast path wait longer too.
DEFAULT_OCR_TIMEOUT_SECONDS = 180

SUPPORTED_MODEL_PROVIDERS = {"ollama"}


class ConfigError(Exception):
    """Raised for an internally inconsistent config - always at startup, never per-request."""


@dataclass(frozen=True)
class OcrSettings:
    """Scanned-PDF OCR fallback settings - nested under `stirling:` since OCR
    is entirely a Stirling capability, not a separate provider.
    """

    enabled: bool = DEFAULT_OCR_ENABLED
    languages: tuple[str, ...] = DEFAULT_OCR_LANGUAGES
    min_meaningful_characters: int = DEFAULT_OCR_MIN_MEANINGFUL_CHARACTERS
    timeout_seconds: int = DEFAULT_OCR_TIMEOUT_SECONDS


@dataclass(frozen=True)
class StirlingSettings:
    base_url: str
    api_key: str
    timeout_seconds: int
    ocr: OcrSettings = OcrSettings()


@dataclass(frozen=True)
class OllamaSettings:
    """Provider-wide Ollama connection settings. No model lives here.

    `keep_alive` is infrastructure config, not a per-request/per-client
    choice (task §6/§8) - it is resolved once here and handed to the single
    OllamaClient instance at startup (system/main.py), which then applies it
    identically to every /api/generate call (analyze and assistant alike,
    no route-specific duplication)."""

    base_url: str
    keep_alive: str = DEFAULT_OLLAMA_KEEP_ALIVE


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
class PromptSettings:
    """Where system/ai/prompt_registry.py's PromptRegistry reads/writes. See
    that module's docstring for the base/runtime layering."""

    base_dir: Path = DEFAULT_PROMPTS_BASE_DIR
    runtime_dir: Path = DEFAULT_PROMPTS_RUNTIME_DIR
    max_content_length: int = DEFAULT_PROMPT_MAX_CONTENT_LENGTH


@dataclass(frozen=True)
class ClientServices:
    documents: bool = False
    # Defaults to False when a client config predates V2 (or simply omits
    # the key) - AI access is always an explicit opt-in, never inherited.
    ai: bool = False
    # V2.2: Prompt Lab (draft-test/save/activate prompt versions) and the
    # assistant routing endpoint - both separate, explicit opt-ins from
    # `ai`. A client with `ai: true` alone gets neither: it can call
    # /documents/analyze but not touch a prompt version or the assistant
    # endpoint, and a prompt_lab/assistant client still needs `ai: true`
    # too wherever an actual Ollama call is involved (see auth.py).
    prompt_lab: bool = False
    assistant: bool = False


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
    prompts: PromptSettings = PromptSettings()
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


def _validate_assistant_mode_routing(models: dict[str, ModelProfile]) -> None:
    for mode_name, mode in ASSISTANT_MODES.items():
        if mode.model_profile not in models:
            raise ConfigError(
                f"Assistant mode '{mode_name}' defaults to model profile '{mode.model_profile}', "
                "which is not defined under 'models'."
            )


def _validate_prompt_defaults(prompts: PromptSettings) -> None:
    """Every mode's (extraction and assistant) repo-shipped default prompt
    file must actually exist and be readable - fail the process at
    startup, never a per-request 500. Runs unconditionally (not gated by
    `_is_ai_configured`): these are committed files, not runtime config,
    so a missing one is always a deployment defect worth catching
    immediately - this mirrors the crash-at-import behavior the eager
    Mode.prompt_template loading used to have before the Prompt Lab made
    prompt loading lazy (see system/ai/prompt_registry.py)."""
    registry = PromptRegistry(prompts.base_dir, prompts.runtime_dir, prompts.max_content_length)
    for mode_name, mode in MODES.items():
        if not registry.default_exists(mode_name, mode.default_prompt_version):
            raise ConfigError(
                f"Mode '{mode_name}' has no base prompt file for its default version "
                f"'{mode.default_prompt_version}' under {prompts.base_dir / mode_name}."
            )
    for mode_name, mode in ASSISTANT_MODES.items():
        subdir = f"assistant/{mode_name}"
        if not registry.default_exists(subdir, mode.default_prompt_version):
            raise ConfigError(
                f"Assistant mode '{mode_name}' has no base prompt file for its default version "
                f"'{mode.default_prompt_version}' under {prompts.base_dir / subdir}."
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
    return any(
        client.services.ai or client.services.assistant or client.model_overrides
        for client in clients.values()
    )


def validate_ai_config(settings: Settings) -> None:
    """Fail-fast validation of model profiles and mode/client routing.

    Raises ConfigError on the first inconsistency found. Intended to be
    called once at startup (see load_settings()) - a broken AI config
    must prevent the process from starting, not surface as a per-request
    500 the first time a client happens to call /analyze or
    /assistant/query.
    """
    _validate_model_profile_shapes(settings.models)
    _validate_mode_routing(settings.models)
    _validate_assistant_mode_routing(settings.models)
    _validate_client_overrides(settings.clients, settings.models)


def load_settings() -> Settings:
    config_path = os.environ.get("DOCPIPE_CONFIG", DEFAULT_CONFIG_PATH)
    raw = _load_yaml(config_path)

    raw_server = raw.get("server") or {}
    server = ServerSettings(
        max_file_size_mb=int(raw_server.get("max_file_size_mb", DEFAULT_MAX_FILE_SIZE_MB)),
    )

    raw_stirling = raw.get("stirling") or {}
    raw_ocr = raw_stirling.get("ocr") or {}
    ocr = OcrSettings(
        enabled=bool(raw_ocr.get("enabled", DEFAULT_OCR_ENABLED)),
        languages=tuple(raw_ocr.get("languages") or DEFAULT_OCR_LANGUAGES),
        min_meaningful_characters=int(
            raw_ocr.get("min_meaningful_characters", DEFAULT_OCR_MIN_MEANINGFUL_CHARACTERS)
        ),
        timeout_seconds=int(raw_ocr.get("timeout_seconds", DEFAULT_OCR_TIMEOUT_SECONDS)),
    )
    stirling = StirlingSettings(
        base_url=os.environ.get("STIRLING_URL", raw_stirling.get("base_url", "")),
        api_key=os.environ.get("STIRLING_API_KEY", raw_stirling.get("api_key", "")),
        timeout_seconds=int(
            os.environ.get(
                "STIRLING_TIMEOUT",
                raw_stirling.get("timeout_seconds", DEFAULT_STIRLING_TIMEOUT_SECONDS),
            )
        ),
        ocr=ocr,
    )

    raw_ollama = raw.get("ollama") or {}
    raw_keep_alive = raw_ollama.get("keep_alive")
    keep_alive = DEFAULT_OLLAMA_KEEP_ALIVE if raw_keep_alive is None else str(raw_keep_alive)
    if not OLLAMA_KEEP_ALIVE_PATTERN.match(keep_alive):
        raise ConfigError(
            f"ollama.keep_alive has an invalid value {keep_alive!r} - expected a positive Ollama "
            "duration such as '30s', '5m', '15m', '1h', or a plain positive integer number of seconds."
        )
    ollama = OllamaSettings(
        base_url=os.environ.get("OLLAMA_URL", raw_ollama.get("base_url", "")),
        keep_alive=keep_alive,
    )

    raw_prompts = raw.get("prompts") or {}
    prompts_base_dir = raw_prompts.get("base_dir")
    prompts_runtime_dir = raw_prompts.get("runtime_dir")
    prompts = PromptSettings(
        base_dir=Path(prompts_base_dir) if prompts_base_dir else DEFAULT_PROMPTS_BASE_DIR,
        runtime_dir=Path(prompts_runtime_dir) if prompts_runtime_dir else DEFAULT_PROMPTS_RUNTIME_DIR,
        max_content_length=int(raw_prompts.get("max_content_length", DEFAULT_PROMPT_MAX_CONTENT_LENGTH)),
    )

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
                prompt_lab=bool(raw_services.get("prompt_lab", False)),
                assistant=bool(raw_services.get("assistant", False)),
            ),
            model_overrides=_parse_model_overrides(client_id, raw_client.get("model_overrides")),
        )

    settings = Settings(
        server=server, stirling=stirling, ollama=ollama, prompts=prompts, models=models, clients=clients
    )

    # Unconditional (see _validate_prompt_defaults docstring) - unlike
    # validate_ai_config() below, this isn't about whether AI is
    # configured, it's "are the repo-shipped prompt files this process
    # ships with actually intact".
    _validate_prompt_defaults(prompts)

    if _is_ai_configured(raw_models, clients):
        validate_ai_config(settings)

    return settings
