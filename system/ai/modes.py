"""The AI mode registry.

Clients never choose a prompt, a model, or a schema directly - they only
ever name one of these pre-registered, server-controlled modes. This is
the single place that maps a mode name to its response JSON Schema and
its *default* model profile/prompt version. Adding a mode means adding an
entry here plus its schema/prompt files under system/prompts/ - nothing
else in the request/response pipeline needs to change.

`model_profile` here is a profile NAME (e.g. "light"), never a concrete
model - "the client selects a task, DocPipe selects the model" holds one
level deeper too: even the mode registry doesn't hardcode a vendor/model
string, only which resource/quality class a mode defaults to. The actual
model behind that name is config (see config.py's `models:` section) and
can change without touching this file. A client-specific override of
this default (config.py's ClientConfig.model_overrides) and the final
name -> ModelProfile lookup both happen in system/ai/resolver.py - this
file only defines the registry, not the routing/override logic, so that
logic exists in exactly one place.

Prompt TEXT is intentionally not loaded here (V2.2, Prompt Lab): only
`default_prompt_version` is - the version this mode falls back to before
anyone has ever activated a different one via the Lab, and the version
whose base file system/ai/config.py's startup check requires to actually
exist. The active version and its content are resolved per-request by
system/ai/prompt_registry.py, never cached at import time - see
docs/prompt-lab.md. The response schema, by contrast, is NOT
Lab-editable (see that doc's "Not implemented") and is loaded eagerly
here exactly as before - a broken/missing schema.json must still crash
the process at startup, not surface as a per-request 500.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Mode:
    name: str
    default_prompt_version: str
    response_schema: dict
    max_input_length: int
    # Default model profile NAME for this mode (see module docstring).
    model_profile: str
    description: str = ""


def _load_mode(
    name: str, default_prompt_version: str, max_input_length: int, model_profile: str, description: str
) -> Mode:
    schema_path = PROMPTS_DIR / name / "schema.json"
    response_schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return Mode(
        name=name,
        default_prompt_version=default_prompt_version,
        response_schema=response_schema,
        max_input_length=max_input_length,
        model_profile=model_profile,
        description=description,
    )


# Conservative, model-independent input caps (no chunking in V2 - a text
# longer than this is rejected outright rather than silently truncated).
_MAX_INPUT_LENGTH = 80_000

MODES: dict[str, Mode] = {
    # Maintenance reports are mostly straightforward structured extraction
    # (a handful of fields from a short-to-medium service report) - the
    # cheapest/fastest profile is the sensible default.
    "maintenance_extraction": _load_mode(
        "maintenance_extraction",
        "v1",
        _MAX_INPUT_LENGTH,
        model_profile="light",
        description="Extract structured maintenance/service facts from a maintenance document.",
    ),
    # Inspection reports vary more in structure and wording and often need
    # more semantic judgement (e.g. distinguishing a real defect finding
    # from boilerplate text) - defaults one class up.
    "inspection_extraction": _load_mode(
        "inspection_extraction",
        "v1",
        _MAX_INPUT_LENGTH,
        model_profile="standard",
        description="Extract structured inspection facts from an inspection/certification document.",
    ),
}


def get_mode(name: str) -> Mode | None:
    return MODES.get(name)
