"""Measuring wrappers around the existing upstream clients.

Installed in system/main.py's lifespan ONLY when monitoring is enabled, so
with monitoring disabled the routes talk to exactly the same client objects
as before. The wrapped clients (system/services/stirling.py,
system/services/ollama.py) are not modified: the production image carries
a local patch in ollama.py that must keep applying cleanly.

Every call is delegated unchanged - same arguments, same return value, same
exceptions. The wrappers only time the call and read the Ollama timing
metadata the client already fills into its `timing` dict (Ollama's own
response fields: load/prompt-eval/eval durations and token counts). They
never issue a request of their own.
"""

from __future__ import annotations

import time

from system.ai.extraction_runner import call_token_counts
from system.monitoring import context, registry
from system.monitoring.context import StepRecord


def _outcome(exc: BaseException | None) -> str:
    if exc is None:
        return registry.OUTCOME_OK
    return registry.outcome(getattr(exc, "code", None))


class MonitoredStirlingClient:
    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()

    def _timed(self, step: str, fn, *args, **kwargs):
        metrics = context.current()
        if metrics is None:
            return fn(*args, **kwargs)
        started = time.perf_counter()
        error: BaseException | None = None
        try:
            return fn(*args, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            context.add_step(
                metrics,
                StepRecord(
                    step=step,
                    outcome=_outcome(error),
                    duration_ms=round((time.perf_counter() - started) * 1000, 3),
                ),
            )

    def extract_text(self, *args, **kwargs):
        return self._timed("stirling.extract_text", self._inner.extract_text, *args, **kwargs)

    def ocr_pdf(self, *args, **kwargs):
        return self._timed("stirling.ocr", self._inner.ocr_pdf, *args, **kwargs)

    def convert_to_pdf(self, *args, **kwargs):
        return self._timed("stirling.convert", self._inner.convert_to_pdf, *args, **kwargs)


def _sum(timing: dict, suffix: str) -> float | None:
    """Sum of an Ollama-reported duration over primary + repair attempts;
    None unless every attempt that ran reported it."""
    phases = [p for p in ("primary", "repair") if f"ollama_{p}_duration_ms" in timing]
    values = [timing.get(f"ollama_{p}_{suffix}") for p in phases]
    if not phases or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in values):
        return None
    return round(float(sum(values)), 3)


class MonitoredOllamaClient:
    def __init__(self, inner, known_models: frozenset[str], monitor=None) -> None:
        self._inner = inner
        self._known_models = known_models
        self._monitor = monitor  # for the running-AI-step list (Phase M5)

    def _begin(self, metrics, step: str, model: object) -> int | None:
        """Mark a real Ollama call as running. Never raises."""
        if self._monitor is None:
            return None
        try:
            return self._monitor.recorder.ai_begin(
                metrics.request_id,
                self._monitor.authenticated_client(metrics),
                self._model(model),
                registry.ai_operation(metrics.route, step),
                step,
                self._monitor._clock(),
            )
        except Exception:
            metrics.hook_failures += 1
            return None

    def _end(self, token) -> None:
        try:
            if self._monitor is not None:
                self._monitor.recorder.ai_end(token)
        except Exception:
            pass

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()

    def _model(self, model: object) -> str:
        return model if isinstance(model, str) and model in self._known_models else registry.UNREGISTERED

    def generate_structured(self, prompt, schema, *, timing: dict | None = None, **kwargs):
        metrics = context.current()
        if metrics is None:
            return self._inner.generate_structured(prompt, schema, timing=timing, **kwargs)
        observed = timing if timing is not None else {}
        started = time.perf_counter()
        error: BaseException | None = None
        token = self._begin(metrics, "ai.generate", kwargs.get("model"))
        try:
            return self._inner.generate_structured(prompt, schema, timing=observed, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._end(token)
            try:
                tokens = call_token_counts(observed, succeeded=error is None)
                total = observed.get("ollama_duration_ms")
                context.add_step(
                    metrics,
                    StepRecord(
                        step="ai.generate",
                        outcome=_outcome(error),
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        ollama_total_ms=float(total) if isinstance(total, (int, float)) else None,
                        ollama_load_ms=_sum(observed, "load_ms"),
                        ollama_prompt_eval_ms=_sum(observed, "prompt_eval_ms"),
                        ollama_eval_ms=_sum(observed, "eval_ms"),
                        tokens_in=tokens["prompt_eval_count"],
                        tokens_out=tokens["eval_count"],
                        repair_used="ollama_repair_duration_ms" in observed,
                        ai_model=self._model(kwargs.get("model")),
                    ),
                )
            except Exception:
                metrics.hook_failures += 1

    def warm_up(self, *, timing: dict | None = None, **kwargs):
        metrics = context.current()
        if metrics is None:
            return self._inner.warm_up(timing=timing, **kwargs)
        observed = timing if timing is not None else {}
        started = time.perf_counter()
        error: BaseException | None = None
        token = self._begin(metrics, "ai.warmup", kwargs.get("model"))
        try:
            return self._inner.warm_up(timing=observed, **kwargs)
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._end(token)
            try:

                def num(key):
                    value = observed.get(key)
                    return (
                        float(value)
                        if isinstance(value, (int, float)) and not isinstance(value, bool)
                        else None
                    )

                context.add_step(
                    metrics,
                    StepRecord(
                        step="ai.warmup",
                        outcome=_outcome(error),
                        duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        ollama_total_ms=num("ollama_duration_ms"),
                        ollama_load_ms=num("ollama_load_ms"),
                        ollama_prompt_eval_ms=num("ollama_prompt_eval_ms"),
                        ollama_eval_ms=num("ollama_eval_ms"),
                        ai_model=self._model(kwargs.get("model")),
                    ),
                )
            except Exception:
                metrics.hook_failures += 1
