"""Turns a Mode + client-supplied context/text into the final prompt sent to Ollama."""

from __future__ import annotations

from typing import Any

from system.ai.modes import Mode


def render_context_block(context: dict[str, Any]) -> str:
    if not context:
        return "(none provided)"
    return "\n".join(f"{key}: {value}" for key, value in context.items())


def build_prompt(mode: Mode, context: dict[str, Any], text: str) -> str:
    return mode.prompt_template.format(context_block=render_context_block(context), text=text)
