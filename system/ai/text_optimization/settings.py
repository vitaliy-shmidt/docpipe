"""Config shape for text optimization (`text_optimization:` in config.yaml,
parsed in system/config.py) and the registry of modes that support it.

Kept free of any system.config import so config.py can import it without
a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from system.ai.text_optimization.contract import (
    CONTRACT_DEPENDENT_FIELDS,
    CONTRACT_GROUPS,
    CONTRACT_GROUPS_V4,
    CONTRACT_LIST_FIELDS_V4,
    CONTRACT_OUTPUT_RESERVE_TOKENS_V4,
    ListMergeRule,
)
from system.ai.text_optimization.passages import KeywordGroup

# Ollama's documented default context length when a request sends no
# num_ctx. Used as the per-call token budget for any model profile without
# an explicit `context_window_tokens` - deliberately the small, safe value.
DEFAULT_ASSUMED_CONTEXT_WINDOW_TOKENS = 4096


@dataclass(frozen=True)
class TextOptimizationSettings:
    enabled: bool = True
    # Normalized text up to this size goes to the model complete (strategy
    # "direct") - a small contract is never reduced.
    direct_limit_chars: int = 8000
    # Above direct_limit (Planner V2): mandatory passages (head + significant
    # hits) go into ONE call whenever they fit the real per-call budget
    # (strategy "relevance"). target_chars is only the SOFT limit for
    # supporting context around them - not a fill target, not a chunking
    # trigger.
    target_chars: int = 7000
    # If the mandatory passages do not fit one call, one call per field
    # group (strategy "chunked"); chunk_target_chars is each chunk's soft
    # limit for supporting context.
    chunking_enabled: bool = True
    chunk_target_chars: int = 4500
    # Supporting context (weak hits, larger neighbors) added on top of the
    # mandatory passages per call, at most - within the soft limits above.
    supporting_context_chars: int = 1500
    # Always-kept document start (parties, number, subject) and, budget
    # permitting, end (signatures, annexes).
    head_chars: int = 1500
    tail_chars: int = 600
    # Single-call strategies: on ai_timeout, one reduced call with the
    # remaining time budget. The first call then only gets this share of
    # the profile timeout, so both fit inside it (see extraction_runner.py).
    timeout_fallback_enabled: bool = True
    primary_timeout_ratio: float = 0.65
    assumed_context_window_tokens: int = DEFAULT_ASSUMED_CONTEXT_WINDOW_TOKENS


# Same as planner.OUTPUT_RESERVE_TOKENS (kept here to avoid an import cycle;
# a test pins both to the same value).
DEFAULT_OUTPUT_RESERVE_TOKENS = 512
_VERSION_NUMBER = re.compile(r"^v([1-9][0-9]*)$")


@dataclass(frozen=True)
class OptimizableMode:
    groups: tuple[KeywordGroup, ...]
    dependent_fields: dict[str, str]
    # Array fields the chunk merge unions instead of the scalar rule.
    list_fields: dict[str, ListMergeRule] = field(default_factory=dict)
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS
    # (N, variant) for prompt versions >= vN with their own schema
    # (modes.Mode.schema_versions) - same selection rule, ascending.
    versions: tuple[tuple[int, "OptimizableMode"], ...] = ()

    def for_version(self, prompt_version: str | None) -> "OptimizableMode":
        match = _VERSION_NUMBER.match(prompt_version) if isinstance(prompt_version, str) else None
        chosen = self
        if match:
            for minimum, variant in self.versions:
                if int(match.group(1)) >= minimum:
                    chosen = variant
        return chosen


# Only modes listed here can be configured under text_optimization:. Other
# extraction modes keep their unchanged single-call path.
OPTIMIZABLE_MODES: dict[str, OptimizableMode] = {
    "contract_extraction": OptimizableMode(
        groups=CONTRACT_GROUPS,
        dependent_fields=CONTRACT_DEPENDENT_FIELDS,
        versions=(
            (
                4,
                OptimizableMode(
                    groups=CONTRACT_GROUPS_V4,
                    dependent_fields=CONTRACT_DEPENDENT_FIELDS,
                    list_fields=CONTRACT_LIST_FIELDS_V4,
                    output_reserve_tokens=CONTRACT_OUTPUT_RESERVE_TOKENS_V4,
                ),
            ),
        ),
    ),
}


def default_text_optimization() -> dict[str, TextOptimizationSettings]:
    """Enabled by default for every optimizable mode (task: contracts)."""
    return {name: TextOptimizationSettings() for name in OPTIMIZABLE_MODES}
