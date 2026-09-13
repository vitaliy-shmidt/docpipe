"""The AI mode registry.

Clients never choose a prompt, a model, or a schema directly - they only
ever name one of these pre-registered, server-controlled modes. This is
the single place that maps a mode name to its versioned prompt template
and its response JSON Schema. Adding a mode means adding an entry here
plus its prompt/schema files under system/prompts/ - nothing else in the
request/response pipeline needs to change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


@dataclass(frozen=True)
class Mode:
    name: str
    prompt_version: str
    prompt_template: str
    response_schema: dict
    max_input_length: int
    description: str = ""


def _load_mode(name: str, prompt_version: str, max_input_length: int, description: str) -> Mode:
    mode_dir = PROMPTS_DIR / name
    prompt_template = (mode_dir / f"{prompt_version}.txt").read_text(encoding="utf-8")
    response_schema = json.loads((mode_dir / "schema.json").read_text(encoding="utf-8"))
    return Mode(
        name=name,
        prompt_version=prompt_version,
        prompt_template=prompt_template,
        response_schema=response_schema,
        max_input_length=max_input_length,
        description=description,
    )


# Conservative, model-independent input caps (no chunking in V2 - a text
# longer than this is rejected outright rather than silently truncated).
_MAX_INPUT_LENGTH = 80_000

MODES: dict[str, Mode] = {
    "maintenance_extraction": _load_mode(
        "maintenance_extraction",
        "v1",
        _MAX_INPUT_LENGTH,
        "Extract structured maintenance/service facts from a maintenance document.",
    ),
    "inspection_extraction": _load_mode(
        "inspection_extraction",
        "v1",
        _MAX_INPUT_LENGTH,
        "Extract structured inspection facts from an inspection/certification document.",
    ),
}


def get_mode(name: str) -> Mode | None:
    return MODES.get(name)
