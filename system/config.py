"""DocPipe server configuration.

Configuration is loaded once at process startup from a local YAML file.
There is no database and no runtime reload in V1.

Load order / priority (highest wins):
  1. Environment variables (STIRLING_URL, STIRLING_API_KEY, STIRLING_TIMEOUT,
     OLLAMA_URL, OLLAMA_MODEL, OLLAMA_TIMEOUT)
  2. Values from the YAML config file
  3. Built-in defaults

The path to the YAML config file itself is controlled by the
DOCPIPE_CONFIG environment variable and defaults to "config.yaml"
in the current working directory.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

DEFAULT_CONFIG_PATH = "config.yaml"
DEFAULT_MAX_FILE_SIZE_MB = 25
DEFAULT_STIRLING_TIMEOUT_SECONDS = 90
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 120
DEFAULT_OLLAMA_TEMPERATURE = 0.0


@dataclass(frozen=True)
class StirlingSettings:
    base_url: str
    api_key: str
    timeout_seconds: int


@dataclass(frozen=True)
class OllamaSettings:
    base_url: str
    model: str
    timeout_seconds: int
    temperature: float = DEFAULT_OLLAMA_TEMPERATURE


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
    ollama = OllamaSettings(
        base_url=os.environ.get("OLLAMA_URL", raw_ollama.get("base_url", "")),
        model=os.environ.get("OLLAMA_MODEL", raw_ollama.get("model", "")),
        timeout_seconds=int(
            os.environ.get(
                "OLLAMA_TIMEOUT",
                raw_ollama.get("timeout_seconds", DEFAULT_OLLAMA_TIMEOUT_SECONDS),
            )
        ),
        temperature=float(raw_ollama.get("temperature", DEFAULT_OLLAMA_TEMPERATURE)),
    )

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
        )

    return Settings(server=server, stirling=stirling, ollama=ollama, clients=clients)
