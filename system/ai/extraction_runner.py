"""Runs one extraction-mode analysis: prompt rendering, Ollama call(s),
result. Shared by /documents/analyze and /lab/analyze, so a Lab draft is
tested with exactly the same input handling as a real request.

Modes without text optimization (see system/ai/text_optimization/
settings.py OPTIMIZABLE_MODES, or disabled in config) take the unchanged
original path: the raw text, one call, the profile timeout.

Optimized modes (contract_extraction) - docs/contract-text-optimization.md:

- the text is planned (normalize -> direct | relevance | chunked) against a
  per-call budget derived from the model's context window and the measured
  prompt overhead;
- the whole request, however many calls it makes, shares ONE deadline: the
  profile's timeout_seconds. A consumer's own HTTP timeout is sized around
  that value (HubDix: 180s vs. `standard` 150s), so DocPipe still answers
  with a clean ai_timeout before the consumer gives up;
- single-call strategies get one bounded timeout fallback: the first call
  only gets primary_timeout_ratio of the budget; if it times out, one
  smaller relevance selection runs with the time left. Never the same large
  prompt again, never more than one fallback;
- chunked runs its 2-3 calls sequentially against the same deadline and
  merges deterministically (system/ai/text_optimization/merge.py). Each
  call keeps generate_structured()'s own single repair attempt; the merged
  result is validated against the schema without any further AI call.

The same resolved model profile is used for every call - no per-chunk model.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import jsonschema

from system.ai.prompting import build_prompt
from system.ai.text_optimization.merge import merge_results
from system.ai.text_optimization.planner import (
    STRATEGY_CHUNKED,
    PlannedCall,
    call_budget_chars,
    estimate_tokens,
    plan_input,
    plan_reduced,
)
from system.ai.text_optimization.settings import OPTIMIZABLE_MODES, TextOptimizationSettings
from system.config import ModelProfile
from system.errors import DocPipeError

# Below this much remaining time a fallback call is not even attempted -
# it could not realistically finish (prompt evaluation alone takes longer).
MIN_FALLBACK_SECONDS = 15
# The fallback call's text budget relative to the first attempt's text -
# a real reduction, not a near-identical retry.
FALLBACK_TEXT_RATIO = 0.5
# Below this, a text is too small for a halved fallback to be meaningfully
# faster - such a request keeps its full timeout and gets no fallback.
# Above it, the second attempt is cheap: the model is loaded by then and
# Ollama reuses the cached prompt prefix (identical template + context).
MIN_FALLBACK_SOURCE_CHARS = 2000


@dataclass
class ExtractionOutcome:
    result: dict[str, Any]
    # Size/strategy/timing diagnostics - None for non-optimized modes.
    # Never contains document text.
    optimization: dict[str, Any] | None
    # Summed Ollama timing over all calls (same keys analyze always logged).
    timing: dict[str, Any]


def run_extraction(
    *,
    mode,
    prompt_template: str,
    context: dict[str, Any],
    text: str,
    profile: ModelProfile,
    ollama_client,
    optimization_settings: dict[str, TextOptimizationSettings],
    observed: dict | None = None,
) -> ExtractionOutcome:
    """`observed`, if given, is filled in place with "optimization" and
    "timing" as soon as they exist - so a caller can still log sizes and
    durations when a call fails with a DocPipeError."""
    observed = {} if observed is None else observed
    settings = optimization_settings.get(mode.name)
    if settings is None or not settings.enabled or mode.name not in OPTIMIZABLE_MODES:
        timing: dict = {}
        observed["optimization"] = None
        observed["timing"] = timing
        result = ollama_client.generate_structured(
            build_prompt(prompt_template, context, text),
            mode.response_schema,
            model=profile.model,
            timeout_seconds=profile.timeout_seconds,
            temperature=profile.temperature,
            timing=timing,
            num_ctx=profile.context_window_tokens,
        )
        return ExtractionOutcome(result=result, optimization=None, timing=timing)

    return _run_optimized(mode, prompt_template, context, text, profile, ollama_client, settings, observed)


def _run_optimized(
    mode, prompt_template, context, text, profile, ollama_client, settings, observed
) -> ExtractionOutcome:
    deadline = time.monotonic() + profile.timeout_seconds
    optimizable = OPTIMIZABLE_MODES[mode.name]
    overhead_chars = len(build_prompt(prompt_template, context, ""))
    context_window = profile.context_window_tokens or settings.assumed_context_window_tokens
    budget = call_budget_chars(context_window, overhead_chars)

    plan = plan_input(text, optimizable.groups, settings, budget)
    stats = dict(plan.stats)
    stats.update(
        {
            "context_window_tokens": context_window,
            "estimated_prompt_tokens": estimate_tokens(
                overhead_chars + max((len(call.text) for call in plan.calls), default=0)
            ),
            "ai_calls": 0,
            "fallback_used": False,
            "merge_conflicts": 0,
        }
    )
    observed["optimization"] = stats
    observed["timing"] = {}
    calls_timing: list[dict] = []

    def run_call(call: PlannedCall, timeout_seconds: float) -> dict:
        call_timing: dict = {}
        calls_timing.append(call_timing)
        stats["ai_calls"] += 1
        return ollama_client.generate_structured(
            build_prompt(prompt_template, context, call.text),
            mode.response_schema,
            model=profile.model,
            timeout_seconds=timeout_seconds,
            temperature=profile.temperature,
            timing=call_timing,
            num_ctx=profile.context_window_tokens,
            deadline=deadline,
        )

    try:
        if plan.strategy == STRATEGY_CHUNKED:
            results = [(call.fields, run_call(call, profile.timeout_seconds)) for call in plan.calls]
            result, conflicts = merge_results(results, mode.response_schema, optimizable.dependent_fields)
            stats["merge_conflicts"] = len(conflicts)
            try:
                jsonschema.validate(result, mode.response_schema)
            except jsonschema.ValidationError as exc:
                raise DocPipeError(
                    "ai_invalid_response", "The AI model did not return a valid structured response."
                ) from exc
        else:
            result = _run_single_with_fallback(
                plan, settings, optimizable, profile, deadline, run_call, stats
            )
    finally:
        timing = _sum_timing(calls_timing)
        observed["timing"] = timing
        stats["ai_ms"] = timing.get("ollama_duration_ms")

    return ExtractionOutcome(result=result, optimization=stats, timing=timing)


def _run_single_with_fallback(plan, settings, optimizable, profile, deadline, run_call, stats) -> dict:
    call = plan.calls[0]
    # Decided up front: only split the budget if the fallback's share can
    # actually hold a call - otherwise the first call keeps the full timeout.
    fallback_possible = (
        settings.timeout_fallback_enabled
        and len(call.text) >= MIN_FALLBACK_SOURCE_CHARS
        and profile.timeout_seconds * (1 - settings.primary_timeout_ratio) >= MIN_FALLBACK_SECONDS
    )
    primary_timeout = (
        profile.timeout_seconds * settings.primary_timeout_ratio
        if fallback_possible
        else profile.timeout_seconds
    )
    try:
        return run_call(call, primary_timeout)
    except DocPipeError as exc:
        remaining = deadline - time.monotonic()
        if exc.code != "ai_timeout" or not fallback_possible or remaining < MIN_FALLBACK_SECONDS:
            raise
    reduced = plan_reduced(
        plan.normalized_text,
        optimizable.groups,
        settings,
        max(int(len(call.text) * FALLBACK_TEXT_RATIO), 1000),
    )
    stats["fallback_used"] = True
    stats["fallback_chars"] = len(reduced.text)
    return run_call(reduced, deadline - time.monotonic())


def _sum_timing(calls_timing: list[dict]) -> dict:
    """Same top-level keys a single call has always produced (analyze's log
    line reads ollama_duration_ms / ollama_repair_duration_ms)."""
    if len(calls_timing) == 1:
        return dict(calls_timing[0])
    total: dict = {}
    for key in ("ollama_primary_duration_ms", "ollama_repair_duration_ms", "ollama_duration_ms"):
        values = [t[key] for t in calls_timing if key in t]
        if values:
            total[key] = round(sum(values), 1)
    return total
