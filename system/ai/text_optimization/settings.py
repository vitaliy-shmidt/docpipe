"""Config shape for text optimization (`text_optimization:` in config.yaml,
parsed in system/config.py) and the registry of modes that support it.

Kept free of any system.config import so config.py can import it without
a cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

from system.ai.text_optimization.contract import CONTRACT_DEPENDENT_FIELDS, CONTRACT_GROUPS
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
    # Above direct_limit: relevant passages are selected up to this size for
    # a single call (strategy "relevance").
    target_chars: int = 7000
    # If the relevant passages do not fit target_chars, one call per field
    # group (strategy "chunked"), each with at most this many characters.
    chunking_enabled: bool = True
    chunk_target_chars: int = 4500
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


@dataclass(frozen=True)
class OptimizableMode:
    groups: tuple[KeywordGroup, ...]
    dependent_fields: dict[str, str]


# Only modes listed here can be configured under text_optimization:. Other
# extraction modes keep their unchanged single-call path.
OPTIMIZABLE_MODES: dict[str, OptimizableMode] = {
    "contract_extraction": OptimizableMode(
        groups=CONTRACT_GROUPS, dependent_fields=CONTRACT_DEPENDENT_FIELDS
    ),
}


def default_text_optimization() -> dict[str, TextOptimizationSettings]:
    """Enabled by default for every optimizable mode (task: contracts)."""
    return {name: TextOptimizationSettings() for name in OPTIMIZABLE_MODES}
