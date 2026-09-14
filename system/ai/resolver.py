"""Resolves a Mode (or AssistantMode) + calling client to a concrete ModelProfile.

The one and only place that implements "client selects the task, DocPipe
selects the model": routes/analyze.py calls this instead of ever reading
a client-supplied model name (there isn't one - AnalyzeRequest has no
such field at all). routes/assistant.py reuses it too (see `_ModeLike`
below) - an AssistantMode is routed to, never client-selected, but the
"whoever picked the mode does not get to pick the model" resolution logic
is identical either way, so it lives here once, not twice.

Resolution order (highest priority first), matching config.py's
fail-fast validation so a valid Settings object can never fail here at
request time for a reason config validation should already have caught:

  1. the calling client's model_overrides for this mode, if present
     (extraction modes only - see config.py's _validate_client_overrides;
     an AssistantMode's name can never appear in model_overrides, so this
     lookup always simply misses for an assistant mode and falls through
     to step 2, which is exactly the desired "no per-client override for
     assistant modes yet" behavior - see system/assistant/modes.py)
  2. the mode's own default model_profile
  3. DocPipeError("model_profile_unavailable") - never a silent fallback
     to some other profile or a hardcoded model.
"""

from __future__ import annotations

from typing import Protocol

from system.config import ClientConfig, ModelProfile, Settings
from system.errors import DocPipeError


class _ModeLike(Protocol):
    """Structural type covering both ai.modes.Mode and
    assistant.modes.AssistantMode - this resolver only ever touches these
    two attributes on whichever one it's given."""

    name: str
    model_profile: str


def resolve_model_profile(
    mode: _ModeLike, client: ClientConfig, settings: Settings
) -> tuple[str, ModelProfile]:
    """Returns (profile_name, profile) for this mode as seen by this client."""
    profile_name = client.model_overrides.get(mode.name, mode.model_profile)
    profile = settings.models.get(profile_name)
    if profile is None:
        raise DocPipeError(
            "model_profile_unavailable",
            f"The configured AI model profile '{profile_name}' is unavailable.",
        )
    return profile_name, profile
