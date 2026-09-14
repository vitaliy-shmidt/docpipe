"""The assistant mode registry.

Same "client never picks a prompt/model directly" principle as
system/ai/modes.py, one level removed: here the client doesn't even pick
the *mode* - system/assistant/router.py does, from the question text. A
client can only ever supply a free-text `question` (and server-trusted
`context`, see system/routes/assistant.py) to POST /api/v1/assistant/query.

`model_profile` is a hardcoded default here, exactly like
system/ai/modes.py's extraction modes - config.yaml's `models:` section
still defines what each profile NAME actually resolves to, and
system/ai/resolver.py's existing mode->profile resolution is reused
as-is (an AssistantMode has the same `name`/`model_profile` shape a
resolver.Mode does). Deliberately no `assistant_modes:` YAML section and
no per-client override: this is a V1 foundation, and every assistant
client shares the same fixed mode->profile defaults - adding a
config-level override later is possible without breaking this shape, but
isn't needed yet (see docs/assistant-routing.md "Not implemented").

`default_prompt_version` works exactly like the extraction registry's
field of the same name (system/ai/modes.py) - resolved at request time by
the same system/ai/prompt_registry.py, just under the "assistant/<mode>"
subdir instead of "<mode>" directly (see system/routes/assistant.py).
"""

from __future__ import annotations

from dataclasses import dataclass

# V1 output is a single free-text answer (see task: "nicht unnötig
# JSON-Schema für natürliche Antwort erzwingen"). Wrapping it in this
# one-field schema - rather than adding a second, schema-less code path to
# OllamaClient - lets every assistant mode reuse generate_structured()
# completely unchanged: the same grammar-constrained decoding, the same
# server-side validation, and the same single repair attempt that every
# extraction mode already gets. Every mode shares this exact schema, so it
# lives here once rather than as a per-mode schema.json file.
ANSWER_SCHEMA: dict = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class AssistantMode:
    name: str
    default_prompt_version: str
    model_profile: str
    description: str = ""


ASSISTANT_MODES: dict[str, AssistantMode] = {
    "hotel_health_summary": AssistantMode(
        name="hotel_health_summary",
        default_prompt_version="v1",
        model_profile="standard",
        description="Summarize a hotel's overall technical state from supplied maintenance/inspection data.",
    ),
    "maintenance_question": AssistantMode(
        name="maintenance_question",
        default_prompt_version="v1",
        model_profile="light",
        description="Answer a question about maintenance items from supplied maintenance context.",
    ),
    "inspection_question": AssistantMode(
        name="inspection_question",
        default_prompt_version="v1",
        model_profile="light",
        description="Answer a question about inspections from supplied inspection context.",
    ),
    "contract_question": AssistantMode(
        name="contract_question",
        default_prompt_version="v1",
        model_profile="light",
        description="Answer a question about contracts from supplied contract context.",
    ),
    "document_question": AssistantMode(
        name="document_question",
        default_prompt_version="v1",
        model_profile="standard",
        description="Answer a question about a specific document's content from supplied document context.",
    ),
    "general_hotel_question": AssistantMode(
        name="general_hotel_question",
        default_prompt_version="v1",
        model_profile="light",
        description="Fallback for a hotel-related question that doesn't clearly match a more specific mode.",
    ),
}


def get_assistant_mode(name: str) -> AssistantMode | None:
    return ASSISTANT_MODES.get(name)
