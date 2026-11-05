"""contract_extraction quality diagnosis - operator CLI, not an API.

    python -m system.tools.contract_diagnose plan    --text FILE [--prompt v3] [--profile heavy] [--probe S]
    python -m system.tools.contract_diagnose run     --text FILE --prompts v2,v3,v3-output-first
                                                     [--models M1,M2] [--input end-to-end|frozen|both]
                                                     [--frozen-from v2] [--gold FILE] [--profile heavy]
    python -m system.tools.contract_diagnose schema
    python -m system.tools.contract_diagnose explain --result FILE

Inside the DocPipe container on staging:
    docker compose exec -T docpipe python -m system.tools.contract_diagnose plan --text /tmp/contract.txt ...

What it answers (docs/contract-text-optimization.md "Quality diagnosis"):

- plan: EXACTLY which optimized text the model gets for a prompt version
  (same normalize -> planner -> budget path as /documents/analyze), and
  where diagnostic probe strings ("vor Ablauf", "Angebot Nr", "7:00", ...)
  occur in raw / normalized / sent text;
- run: the same contract text through several prompt versions (registry
  versions, transient drafts via `file:PATH`, the built-in ablation
  `v3-output-first`) and several Ollama models. Two inputs, reported
  separately:
  * end-to-end - each prompt plans its own input (a prompt's size changes
    the planner budget), i.e. the production path;
  * frozen     - one optimized input (planned with --frozen-from) sent
    unchanged to every prompt/model: only prompt/model differ;
  per run: raw model JSON, prompt_eval_count, eval_count, duration,
  repair/fallback, and a field check against an optional gold result;
- schema: size/complexity of the response schema;
- explain: how HubDix would display a stored ai_result_json (amount is
  rounded to 2 decimals - an amount of 6.999 shows as "7,00").

Safety: everything goes to STDOUT of the operator's own shell. Nothing is
logged, stored, or sent anywhere but the configured Ollama. Document text
is only printed with --show-text / --show-context. Drafts are never saved
to the Prompt Lab registry; no config, profile or model is changed (a
--models value is only a per-call model name for an already installed
model - nothing is pulled).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from pathlib import Path

from system.ai.extraction_runner import call_token_counts, run_extraction
from system.ai.modes import MODES
from system.ai.prompt_registry import PromptRegistry
from system.ai.prompting import build_prompt
from system.ai.text_optimization.merge import merge_results
from system.ai.text_optimization.normalize import normalize_text
from system.ai.text_optimization.planner import STRATEGY_CHUNKED, call_budget_chars, plan_input
from system.ai.text_optimization.settings import OPTIMIZABLE_MODES

MODE_NAME = "contract_extraction"
OUTPUT_FIRST_ABLATION = "v3-output-first"

# Generic probes: field evidence and known look-alikes. Extend with --probe.
DEFAULT_PROBES = (
    "Vertragsnummer",
    "Vertrag Nr",
    "Angebot Nr",
    "Projekt",
    "Vertragsbeginn",
    "Vertragslaufzeit",
    "vor Ablauf",
    "verlängert",
    "je nachdem",
    "Kündigung",
    "Angebotsgültigkeit",
    "Fälligkeit",
    "Vertragspreis",
    "Jahresrechnung",
    "monatlich",
    "7,00",
    "7:00",
    "24/7",
    "Ansprechpartner",
)


# --- Prompt variants ------------------------------------------------------------


def output_first(template: str) -> str:
    """Ablation: the same prompt with its trailing OUTPUT section moved in
    front of CONTEXT (v2's order). Every line is kept; only the position of
    the output instruction changes. Raises ValueError if there is none."""
    marker = "\n\nOUTPUT\n"
    if marker not in template or "\nCONTEXT:\n" not in template:
        raise ValueError("template has no trailing OUTPUT section to move")
    body, output = template.split(marker, 1)
    head, tail = body.split("\nCONTEXT:\n", 1)
    return f"{head}\nOUTPUT\n{output.rstrip()}\n\nCONTEXT:\n{tail}\n"


def resolve_prompt(spec: str, registry: PromptRegistry, runtime_dir: Path) -> tuple[str, str]:
    """(template, source) for a version ("v2"), a transient draft file
    ("file:/tmp/draft.txt") or a built-in ablation ("v3-output-first").
    `source` tells whether a version came from the runtime directory (a Lab
    save shadows the repo file of the same name) or the shipped base."""
    if spec.startswith("file:"):
        return Path(spec[5:]).read_text(encoding="utf-8"), f"draft {spec[5:]}"
    if spec == OUTPUT_FIRST_ABLATION:
        template, source = resolve_prompt("v3", registry, runtime_dir)
        return output_first(template), f"ablation of v3 ({source})"
    source = "runtime (Lab-saved)" if (runtime_dir / MODE_NAME / f"{spec}.txt").is_file() else "base (repo)"
    return registry.load_prompt(MODE_NAME, spec), source


# --- Planning (identical to the runner) ----------------------------------------------


def plan_for(template: str, context: dict, text: str, profile, opt_settings):
    """Exactly the planning extraction_runner._run_optimized does."""
    overhead = len(build_prompt(template, context, ""))
    window = profile.context_window_tokens or opt_settings.assumed_context_window_tokens
    budget = call_budget_chars(window, overhead)
    groups = OPTIMIZABLE_MODES[MODE_NAME].groups
    return plan_input(text, groups, opt_settings, budget), budget, window


def probe_report(probes, raw: str, normalized: str, sent: str, context_chars: int = 0) -> list[dict]:
    rows = []
    for probe in probes:
        row = {
            "probe": probe,
            "raw": raw.count(probe),
            "normalized": normalized.count(probe),
            "sent": sent.count(probe),
        }
        if context_chars:
            row["sent_context"] = _contexts(sent, probe, context_chars)
        rows.append(row)
    return rows


def _contexts(text: str, probe: str, width: int, limit: int = 5) -> list[str]:
    out, start = [], 0
    while len(out) < limit and (index := text.find(probe, start)) != -1:
        out.append(text[max(0, index - width) : index + len(probe) + width].replace("\n", " ⏎ "))
        start = index + len(probe)
    return out


# --- Running -----------------------------------------------------------------------------


def _profile(settings, name: str, model: str | None, context_window: int | None = None):
    """The configured profile, optionally with another installed model name
    or context window for this diagnosis run only (config is not changed)."""
    profile = settings.models[name]
    if model:
        profile = dataclasses.replace(profile, model=model)
    if context_window:
        profile = dataclasses.replace(profile, context_window_tokens=context_window)
    return profile


def run_end_to_end(template, context, text, profile, client, settings) -> dict:
    observed: dict = {}
    started = time.monotonic()
    try:
        outcome = run_extraction(
            mode=MODES[MODE_NAME],
            prompt_template=template,
            context=context,
            text=text,
            profile=profile,
            ollama_client=client,
            optimization_settings=settings.text_optimization,
            observed=observed,
        )
        result, error = outcome.result, None
    except Exception as exc:  # report, keep the matrix running
        result, error = None, f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"
    stats = observed.get("optimization") or {}
    return {
        "result": result,
        "error": error,
        "optimized_chars": stats.get("optimized_chars"),
        "strategy": stats.get("strategy"),
        "ai_calls": stats.get("ai_calls"),
        "prompt_eval_count": stats.get("prompt_eval_count_total"),
        "eval_count": stats.get("eval_count_total"),
        "repair_used": any(call.get("repair_used") for call in stats.get("calls", [])),
        "fallback_used": stats.get("fallback_used"),
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }


def run_frozen(template, context, frozen_plan, profile, client) -> dict:
    """Sends the frozen plan's call texts unchanged - same merge as the
    runner for chunked plans; no fallback (a timeout is reported as such)."""
    mode = MODES[MODE_NAME]
    results, timings = [], []
    started = time.monotonic()
    error = None
    try:
        for call in frozen_plan.calls:
            timing: dict = {}
            timings.append(timing)
            result = client.generate_structured(
                build_prompt(template, context, call.text),
                mode.response_schema,
                model=profile.model,
                timeout_seconds=profile.timeout_seconds,
                temperature=profile.temperature,
                timing=timing,
                num_ctx=profile.context_window_tokens,
            )
            results.append((call.fields, result))
        if frozen_plan.strategy == STRATEGY_CHUNKED:
            merged, _ = merge_results(
                results, mode.response_schema, OPTIMIZABLE_MODES[MODE_NAME].dependent_fields
            )
        else:
            merged = results[0][1]
    except Exception as exc:
        merged, error = None, f"{type(exc).__name__}: {getattr(exc, 'code', exc)}"
    counts = [call_token_counts(t, succeeded=error is None) for t in timings]
    total = {
        field: sum(c[field] for c in counts) if counts and all(c[field] is not None for c in counts) else None
        for field in ("prompt_eval_count", "eval_count")
    }
    return {
        "result": merged,
        "error": error,
        "optimized_chars": sum(len(call.text) for call in frozen_plan.calls),
        "strategy": frozen_plan.strategy,
        "ai_calls": len(timings),
        "prompt_eval_count": total["prompt_eval_count"],
        "eval_count": total["eval_count"],
        "repair_used": any("ollama_repair_duration_ms" in t for t in timings),
        "fallback_used": False,
        "duration_ms": round((time.monotonic() - started) * 1000, 1),
    }


# --- Gold comparison / explain -----------------------------------------------------------


def check_against_gold(result: dict | None, gold: dict) -> dict:
    """Gold value per field: a literal (exact match, null included),
    {"contains": [...]} (all substrings, case-insensitive) or
    {"any_of": [...]} (one of the literals)."""
    checks = {}
    for field, expected in gold.items():
        actual = None if result is None else result.get(field)
        if isinstance(expected, dict) and "contains" in expected:
            ok = isinstance(actual, str) and all(s.lower() in actual.lower() for s in expected["contains"])
        elif isinstance(expected, dict) and "any_of" in expected:
            ok = actual in expected["any_of"]
        else:
            ok = actual == expected
        checks[field] = {"ok": ok, "actual": actual}
    return checks


def hubdix_amount_display(amount) -> str | None:
    """HubDix takes round(float(amount), 2) and formats it German-style."""
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    formatted = f"{round(float(amount), 2):,.2f}"
    return formatted.replace(",", "X").replace(".", ",").replace("X", ".")


def explain_result(result: dict) -> dict:
    amount = result.get("amount")
    notes = []
    if isinstance(amount, float) and amount < 1000 and len(repr(amount).split(".")[-1]) == 3:
        notes.append(
            "amount has exactly 3 decimals - looks like a German thousands separator read as decimal point "
            "(e.g. '6.999,00' -> 6.999)"
        )
    if result.get("notice_period") and result.get("notice_period_value") is None:
        notes.append(
            "notice_period text present but notice_period_value null - HubDix shows no structured notice"
        )
    if result.get("auto_renewal") and result.get("renewal_period_value") is None:
        notes.append("auto_renewal true without renewal_period_value - HubDix shows only 'automatisch'; "
                     "the full clause, if any, is in renewal_terms")  # fmt: skip
    return {
        "amount_raw": repr(amount),
        "hubdix_amount_display": hubdix_amount_display(amount),
        "payment_interval": result.get("payment_interval"),
        "text_fields": {
            k: result.get(k) for k in ("contract_number", "notice_period", "renewal_terms", "notes")
        },
        "structured": {
            k: result.get(k)
            for k in (
                "notice_period_value",
                "notice_period_unit",
                "auto_renewal",
                "renewal_period_value",
                "renewal_period_unit",
                "is_open_ended",
                "start_date",
                "end_date",
            )  # fmt: skip
        },
        "findings": notes,
    }


def schema_audit(schema: dict) -> dict:
    properties = schema.get("properties", {})

    def depth(node, level=0):
        children = [v for v in (node.get("properties") or {}).values() if isinstance(v, dict)]
        return max([depth(c, level + 1) for c in children], default=level)

    nullable = [
        name
        for name, d in properties.items()
        if (isinstance(d.get("type"), list) and "null" in d["type"]) or (None in d.get("enum", []))
    ]
    return {
        "field_count": len(properties),
        "required_count": len(schema.get("required", [])),
        "nullable_count": len(nullable),
        "enum_fields": {
            n: [v for v in d["enum"] if v is not None] for n, d in properties.items() if "enum" in d
        },
        "pattern_fields": [n for n, d in properties.items() if "pattern" in d],
        "number_fields": [n for n, d in properties.items() if "number" in (d.get("type") or [])],
        "fields_with_description": [n for n, d in properties.items() if "description" in d],
        "nesting_depth": depth(schema),
        "additional_properties": schema.get("additionalProperties", True),
        "schema_json_chars": len(json.dumps(schema, ensure_ascii=False)),
    }


# --- CLI -----------------------------------------------------------------------------------


def _settings():
    from system.config import load_settings

    return load_settings()


def _registry(settings) -> PromptRegistry:
    p = settings.prompts
    return PromptRegistry(p.base_dir, p.runtime_dir, p.max_content_length)


def _client(settings):
    from system.services.ollama import OllamaClient

    return OllamaClient(settings.ollama.base_url, keep_alive=settings.ollama.keep_alive)


def _read_text(source: str) -> str:
    """A file path, or "-" for stdin (pipe the text in, no file on the server)."""
    if source == "-":
        return sys.stdin.buffer.read().decode("utf-8")
    return Path(source).read_text(encoding="utf-8")


def cmd_plan(args, settings, out) -> dict:
    text = _read_text(args.text)
    context = json.loads(args.context)
    template, source = resolve_prompt(args.prompt, _registry(settings), Path(settings.prompts.runtime_dir))
    profile = _profile(settings, args.profile, None, args.context_window)
    plan, budget, window = plan_for(template, context, text, profile, settings.text_optimization[MODE_NAME])
    sent = "\n\n".join(call.text for call in plan.calls)
    report = {
        "prompt": args.prompt,
        "prompt_source": source,
        "profile": args.profile,
        "context_window_tokens": window,
        "call_budget_chars": budget,
        "stats": {k: v for k, v in plan.stats.items()},
        "calls": [{"label": c.label, "chars": len(c.text)} for c in plan.calls],
        "probes": probe_report(
            list(DEFAULT_PROBES) + list(args.probe or []),
            text,
            normalize_text(text),
            sent,
            args.context_chars if args.show_context else 0,
        ),
    }
    if args.show_text:
        report["sent_text"] = [{"label": c.label, "text": c.text} for c in plan.calls]
    return report


def cmd_run(args, settings, out) -> dict:
    text = _read_text(args.text)
    context = json.loads(args.context)
    registry, runtime_dir = _registry(settings), Path(settings.prompts.runtime_dir)
    client = _client(settings)
    gold = json.loads(Path(args.gold).read_text(encoding="utf-8")) if args.gold else None
    models = [m for m in (args.models or "").split(",") if m] or [None]
    prompts = [p for p in args.prompts.split(",") if p]
    opt = settings.text_optimization[MODE_NAME]
    runs = []
    frozen_meta = None
    for model in models:
        profile = _profile(settings, args.profile, model, args.context_window)
        if args.warmup:
            client.warm_up(
                model=profile.model,
                timeout_seconds=profile.timeout_seconds,
                num_ctx=profile.context_window_tokens,
            )
        frozen_plan = None
        if args.input in ("frozen", "both"):
            frozen_template, _ = resolve_prompt(args.frozen_from, registry, runtime_dir)
            frozen_plan, budget, _ = plan_for(frozen_template, context, text, profile, opt)
            frozen_meta = {"planned_with": args.frozen_from, "call_budget_chars": budget,
                           "optimized_chars": sum(len(c.text) for c in frozen_plan.calls)}  # fmt: skip
        for spec in prompts:
            template, source = resolve_prompt(spec, registry, runtime_dir)
            for kind in ("end-to-end", "frozen"):
                if args.input not in (kind, "both"):
                    continue
                for repeat in range(args.repeat):
                    if kind == "end-to-end":
                        row = run_end_to_end(template, context, text, profile, client, settings)
                    else:
                        row = run_frozen(template, context, frozen_plan, profile, client)
                    row = {"prompt": spec, "prompt_source": source, "model": profile.model, "input": kind,
                           "repeat": repeat + 1, **row}  # fmt: skip
                    if row["result"] is not None:
                        row["explain"] = explain_result(row["result"])
                    if gold is not None:
                        row["gold_check"] = check_against_gold(row["result"], gold)
                    runs.append(row)
                    print(f"# done: {spec} / {profile.model} / {kind} #{repeat + 1}", file=sys.stderr)
    return {"profile": args.profile, "frozen_input": frozen_meta, "runs": runs}


def main(argv: list[str] | None = None, out=sys.stdout) -> int:
    parser = argparse.ArgumentParser(prog="contract_diagnose", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p):
        p.add_argument("--text", required=True, help="UTF-8 file with the extracted text, or - for stdin")
        p.add_argument(
            "--context", default='{"document_type": "contract"}', help="context JSON (as HubDix sends)"
        )
        p.add_argument("--profile", default="heavy", help="model profile from config.yaml (default: heavy)")
        p.add_argument("--context-window", type=int, help="override the profile's context window (this run)")

    p_plan = sub.add_parser("plan", help="show the optimized model input and probe hits")
    common(p_plan)
    p_plan.add_argument("--prompt", default=MODES[MODE_NAME].default_prompt_version)
    p_plan.add_argument("--probe", action="append", help="additional probe string (repeatable)")
    p_plan.add_argument("--show-context", action="store_true", help="print text around each probe hit")
    p_plan.add_argument("--context-chars", type=int, default=80)
    p_plan.add_argument("--show-text", action="store_true", help="print the full optimized text")

    p_run = sub.add_parser("run", help="prompt x model matrix against the configured Ollama")
    common(p_run)
    p_run.add_argument("--prompts", default="v2,v3", help="versions, file:PATH drafts, v3-output-first")
    p_run.add_argument("--models", default="", help="installed Ollama model names (default: the profile's)")
    p_run.add_argument("--input", choices=("end-to-end", "frozen", "both"), default="both")
    p_run.add_argument("--frozen-from", default="v2", help="prompt version the frozen input is planned with")
    p_run.add_argument("--gold", help="JSON file with expected field values")
    p_run.add_argument("--repeat", type=int, default=1)
    p_run.add_argument("--warmup", action="store_true", help="load each model before its first run")

    sub.add_parser("schema", help="response schema size/complexity")
    p_explain = sub.add_parser("explain", help="explain a stored ai_result_json")
    p_explain.add_argument("--result", required=True, help="file with the ai_result_json")

    args = parser.parse_args(argv)
    if out is sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # report is UTF-8 JSON on every platform
    if args.command == "schema":
        report = schema_audit(MODES[MODE_NAME].response_schema)
    elif args.command == "explain":
        report = explain_result(json.loads(Path(args.result).read_text(encoding="utf-8")))
    else:
        settings = _settings()
        report = cmd_plan(args, settings, out) if args.command == "plan" else cmd_run(args, settings, out)
    out.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
