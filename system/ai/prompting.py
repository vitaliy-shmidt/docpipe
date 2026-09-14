"""Turns a prompt template + client-supplied context/text into the final
prompt sent to Ollama.

Deliberately takes the template as a plain string, not a `Mode` - the
template may be the mode's resolved active version (system/routes/
analyze.py) or a Prompt Lab draft that was never saved anywhere
(system/routes/lab.py). Both go through the exact same rendering, which
is the point: a draft is tested with identical mechanics to a real
request, only the template itself differs.
"""

from __future__ import annotations

from typing import Any

from system.errors import DocPipeError


def render_context_block(context: dict[str, Any]) -> str:
    if not context:
        return "(none provided)"
    return "\n".join(f"{key}: {value}" for key, value in context.items())


def build_prompt(prompt_template: str, context: dict[str, Any], text: str) -> str:
    try:
        return prompt_template.format(context_block=render_context_block(context), text=text)
    except (KeyError, IndexError, ValueError) as exc:
        raise DocPipeError(
            "invalid_prompt",
            "Prompt template is malformed (bad or missing {context_block}/{text} placeholder).",
        ) from exc


def validate_prompt_template(prompt_template: str) -> None:
    """Raises DocPipeError("invalid_prompt") if the template can't be rendered.

    Used before *persisting* a Lab-saved version (system/routes/lab.py) so
    a syntactically broken prompt fails fast at save time, not on some
    later real request - and before running a draft (which build_prompt()
    would catch anyway, but checking first keeps the error path identical
    for both).
    """
    build_prompt(prompt_template, {}, "")
