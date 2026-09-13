"""Resolves a Mode + calling client to a concrete ModelProfile.

The one and only place that implements "client selects the task, DocPipe
selects the model": routes/analyze.py calls this instead of ever reading
a client-supplied model name (there isn't one - AnalyzeRequest has no
such field at all).

Resolution order (highest priority first), matching config.py's
fail-fast validation so a valid Settings object can never fail here at
request time for a reason config validation should already have caught:

  1. the calling client's model_overrides for this mode, if present
  2. the mode's own default model_profile
  3. DocPipeError("model_profile_unavailable") - never a silent fallback
     to some other profile or a hardcoded model.
"""

from __future__ import annotations

from system.ai.modes import Mode
from system.config import ClientConfig, ModelProfile, Settings
from system.errors import DocPipeError


def resolve_model_profile(mode: Mode, client: ClientConfig, settings: Settings) -> tuple[str, ModelProfile]:
    """Returns (profile_name, profile) for this mode as seen by this client."""
    profile_name = client.model_overrides.get(mode.name, mode.model_profile)
    profile = settings.models.get(profile_name)
    if profile is None:
        raise DocPipeError(
            "model_profile_unavailable",
            f"The configured AI model profile '{profile_name}' is unavailable.",
        )
    return profile_name, profile
