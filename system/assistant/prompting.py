"""Turns an assistant prompt template + server-trusted context/question into
the final prompt sent to Ollama.

Deliberately a separate, differently-shaped renderer from
system/ai/prompting.py's `render_context_block()`: extraction context is a
flat bag of short hint strings (`hotel_name: "..."`), while assistant
context is expected to carry whole nested structures (a hotel's data, a
list of maintenance items, ...) - see system/routes/assistant.py. A JSON
dump is a far more reliable way to hand a model a nested structure than a
flattened `key: value` line ever would be.

Uses the placeholders `{context_block}`/`{question}` (extraction prompts
use `{context_block}`/`{text}` instead - see system/ai/prompting.py) -
the two are intentionally not interchangeable, so an assistant prompt
template and an extraction prompt template can never be swapped in by
accident and silently "half work".
"""

from __future__ import annotations

import json
from typing import Any

from system.errors import DocPipeError


def render_context_block(context: dict[str, Any]) -> str:
    if not context:
        return "(no context provided)"
    return json.dumps(context, indent=2, ensure_ascii=False, default=str)


def render_history_block(history: list[dict[str, str]] | None) -> str:
    """Assistant V2: short conversation history, oldest first.

    Plain "User:/Assistant:" lines - the prompt tells the model to use it
    only to resolve references, never as a source of facts. A v1 template
    without a {history_block} placeholder simply ignores it (str.format
    ignores unused keyword arguments), so v1 prompts keep working unchanged.
    """
    if not history:
        return "(no previous messages)"
    labels = {"user": "User", "assistant": "Assistant"}
    return chr(10).join(f"{labels.get(turn['role'], 'User')}: {turn['content']}" for turn in history)


def build_assistant_prompt(
    prompt_template: str, context: dict[str, Any], question: str, history: list[dict[str, str]] | None = None
) -> str:
    try:
        return prompt_template.format(
            context_block=render_context_block(context),
            question=question,
            history_block=render_history_block(history),
        )
    except (KeyError, IndexError, ValueError) as exc:
        raise DocPipeError(
            "invalid_prompt",
            "Prompt template is malformed (bad or missing {context_block}/{question} placeholder).",
        ) from exc
