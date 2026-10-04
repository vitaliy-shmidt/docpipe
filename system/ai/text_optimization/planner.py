"""Decides how much of a document an extraction call gets to see.

    raw text -> normalize -> direct | relevance | chunked

- direct:    normalized text fits the direct limit -> complete text, 1 call
- relevance: verbatim relevant passages (+ head/tail) fit the target -> 1 call
- chunked:   they don't -> one call per field group, each only with that
             group's passages (2-3 calls, never the full text per chunk)

Every limit is additionally capped by the per-call token budget derived from
the model's context window and the measured prompt overhead, so the text
slot can never fill the whole window.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.passages import KeywordGroup, score_blocks, segment_blocks, select
from system.ai.text_optimization.settings import TextOptimizationSettings

# Rough characters-per-token for German/English text with current BPE
# tokenizers (Qwen/Llama/Mistral land around 3.5-4.5). Deliberately on the
# low side so token counts are over- rather than under-estimated. No
# tokenizer dependency - this only has to be safe, not exact.
CHARS_PER_TOKEN = 3.2
# Room for the model's answer: a full contract_extraction JSON object is
# ~150 (mostly null) to ~350 tokens.
OUTPUT_RESERVE_TOKENS = 512
# Share of the window kept free on top of prompt + text + output - absorbs
# estimation error and most of the one repair attempt (which appends the
# schema and the previous answer to the original prompt).
SAFETY_MARGIN_RATIO = 0.10
# Floor for the per-call text budget, so a very long Lab draft prompt or a
# tiny configured window degrades to small calls instead of empty ones.
MIN_CALL_BUDGET_CHARS = 1500

STRATEGY_DIRECT = "direct"
STRATEGY_RELEVANCE = "relevance"
STRATEGY_CHUNKED = "chunked"


def estimate_tokens(chars: int) -> int:
    return int(chars / CHARS_PER_TOKEN) + 1


def call_budget_chars(context_window_tokens: int, prompt_overhead_chars: int) -> int:
    """Characters of document text one call can carry: context window minus
    prompt overhead (template + context block), output reserve and safety
    margin."""
    usable = context_window_tokens * (1 - SAFETY_MARGIN_RATIO)
    usable -= estimate_tokens(prompt_overhead_chars) + OUTPUT_RESERVE_TOKENS
    return max(MIN_CALL_BUDGET_CHARS, int(usable * CHARS_PER_TOKEN))


@dataclass(frozen=True)
class PlannedCall:
    # "document" for direct/relevance, otherwise the field group name.
    label: str
    text: str
    # Response fields this call is authoritative for; None = all fields.
    fields: tuple[str, ...] | None
    passage_count: int


@dataclass
class InputPlan:
    strategy: str
    calls: list[PlannedCall]
    normalized_text: str
    stats: dict = field(default_factory=dict)


def plan_input(
    raw_text: str,
    groups: tuple[KeywordGroup, ...],
    settings: TextOptimizationSettings,
    budget_chars: int,
) -> InputPlan:
    started_at = time.monotonic()
    normalized = normalize_text(raw_text)
    normalized_at = time.monotonic()

    direct_limit = min(settings.direct_limit_chars, budget_chars)
    dropped = 0
    if len(normalized) <= direct_limit:
        strategy = STRATEGY_DIRECT
        calls = [PlannedCall("document", normalized, None, 1 if normalized else 0)]
    else:
        blocks = segment_blocks(normalized)
        scores = score_blocks(normalized, blocks, groups)
        all_names = tuple(group.name for group in groups)
        target = min(settings.target_chars, budget_chars)
        selection = select(
            normalized,
            blocks,
            scores,
            all_names,
            target,
            head_chars=settings.head_chars,
            tail_chars=settings.tail_chars,
        )
        if selection.complete or not settings.chunking_enabled:
            strategy = STRATEGY_RELEVANCE
            calls = [PlannedCall("document", selection.text, None, selection.passage_count)]
            dropped = selection.hit_blocks - selection.included_hits
        else:
            strategy = STRATEGY_CHUNKED
            calls = []
            chunk_budget = min(settings.chunk_target_chars, budget_chars)
            for group in groups:
                if not group.carries_head and not any(score[group.name] for score in scores):
                    continue  # nothing about this topic anywhere - no call
                group_selection = select(
                    normalized,
                    blocks,
                    scores,
                    (group.name,),
                    chunk_budget,
                    head_chars=settings.head_chars if group.carries_head else 0,
                    tail_chars=settings.tail_chars if group.carries_head else 0,
                )
                dropped += group_selection.hit_blocks - group_selection.included_hits
                calls.append(
                    PlannedCall(group.name, group_selection.text, group.fields, group_selection.passage_count)
                )

    finished_at = time.monotonic()
    stats = {
        "raw_chars": len(raw_text),
        "normalized_chars": len(normalized),
        "optimized_chars": sum(len(call.text) for call in calls),
        "strategy": strategy,
        "passage_count": sum(call.passage_count for call in calls),
        "chunk_count": len(calls) if strategy == STRATEGY_CHUNKED else 0,
        # Keyword-hit passages that did not fit the budget - selection, never
        # a cut through the middle of a passage.
        "dropped_passages": dropped,
        "call_budget_chars": budget_chars,
        "normalization_ms": round((normalized_at - started_at) * 1000, 1),
        "optimization_ms": round((finished_at - normalized_at) * 1000, 1),
    }
    return InputPlan(strategy=strategy, calls=calls, normalized_text=normalized, stats=stats)


def plan_reduced(
    normalized_text: str,
    groups: tuple[KeywordGroup, ...],
    settings: TextOptimizationSettings,
    budget_chars: int,
) -> PlannedCall:
    """The timeout fallback's single, smaller call: the same relevance
    selection over all groups, with a tighter budget and a shorter head."""
    blocks = segment_blocks(normalized_text)
    scores = score_blocks(normalized_text, blocks, groups)
    selection = select(
        normalized_text,
        blocks,
        scores,
        tuple(group.name for group in groups),
        budget_chars,
        head_chars=settings.head_chars // 2,
        tail_chars=0,
    )
    return PlannedCall("document", selection.text, None, selection.passage_count)
