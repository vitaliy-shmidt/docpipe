"""API key authentication.

Clients authenticate with:

    Authorization: Bearer <api-key>

The key is looked up against the clients loaded from the local config
file (see config.py). There is no database and no session state.
"""

from __future__ import annotations

from fastapi import Header, Request

from system.config import ClientConfig, Settings
from system.errors import DocPipeError

AUTH_SCHEME = "Bearer"


def _extract_bearer_token(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != AUTH_SCHEME.lower():
        return None
    token = parts[1].strip()
    return token or None


def get_authenticated_client(
    request: Request,
    authorization: str | None = Header(default=None),
) -> ClientConfig:
    """Resolve the calling client from the Authorization header.

    Raises DocPipeError("unauthorized") if the key is missing, malformed,
    unknown, or belongs to a disabled client.
    """
    settings: Settings = request.app.state.settings

    api_key = _extract_bearer_token(authorization)
    if not api_key:
        raise DocPipeError("unauthorized", "Missing or malformed API key.")

    client = settings.find_client_by_api_key(api_key)
    if client is None or not client.enabled:
        raise DocPipeError("unauthorized", "Invalid API key.")

    request.state.client_id = client.client_id
    return client


def require_documents_service(client: ClientConfig) -> None:
    """Raise DocPipeError("service_disabled") unless documents is enabled for the client."""
    if not client.services.documents:
        raise DocPipeError("service_disabled", "The documents service is not enabled for this client.")


def require_ai_service(client: ClientConfig) -> None:
    """Raise DocPipeError("ai_disabled") unless ai is enabled for the client."""
    if not client.services.ai:
        raise DocPipeError("ai_disabled", "The AI analysis service is not enabled for this client.")


def require_prompt_lab_service(client: ClientConfig) -> None:
    """Raise DocPipeError("prompt_lab_disabled") unless prompt_lab is enabled."""
    if not client.services.prompt_lab:
        raise DocPipeError("prompt_lab_disabled", "The prompt lab is not enabled for this client.")


def require_assistant_service(client: ClientConfig) -> None:
    """Raise DocPipeError("assistant_disabled") unless assistant is enabled."""
    if not client.services.assistant:
        raise DocPipeError("assistant_disabled", "The assistant service is not enabled for this client.")
