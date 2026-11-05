"""system/tools/contract_diagnose.py - the operator diagnosis CLI.

Deterministic only: the tool must reconstruct EXACTLY the model input the
production path builds, freeze one input for a fair prompt comparison,
report prompt sources (runtime shadowing), and never print or log document
text unless asked. Model quality itself is not tested here (mock transport
only checks delivery) - that is the staging diagnosis.
"""

from __future__ import annotations

import dataclasses
import io
import json
import logging

import httpx
import pytest

from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.prompt_registry import PromptRegistry
from system.ai.text_optimization.settings import TextOptimizationSettings
from system.config import ModelProfile
from system.services.ollama import OllamaClient
from system.tools import contract_diagnose as tool
from tests.conftest import _build_test_settings
from tests.contract_samples import SERVICE_CORE_SNIPPETS, build_service_contract

MODE = "contract_extraction"
CONTRACT = MODES[MODE]
V2 = (PROMPTS_DIR / MODE / "v2.txt").read_text(encoding="utf-8")
V3 = (PROMPTS_DIR / MODE / "v3.txt").read_text(encoding="utf-8")
PROFILE = ModelProfile(
    provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=8192
)
OPT = TextOptimizationSettings()
CONTEXT = {"document_type": "contract"}
SECRET = "GEHEIMKLAUSEL-4711"


def _client(answer=None):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        body = (
            answer(payload) if answer else {field: None for field in CONTRACT.response_schema["properties"]}
        )
        return httpx.Response(200, json={"response": json.dumps(body), "done": True, "prompt_eval_count": 100,
                                         "eval_count": 10})  # fmt: skip

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client, requests


def _document(prompt: str) -> str:
    return prompt.split("DOCUMENT TEXT:\n", 1)[1].split("\n\nOUTPUT\n", 1)[0].removesuffix("\n")


# --- Exact reconstruction of the model input ---------------------------------------


@pytest.mark.parametrize("template", [V2, V3], ids=["v2", "v3"])
def test_plan_reconstructs_exactly_what_the_runner_sends(template):
    text = build_service_contract(100)
    client, requests = _client()
    run_extraction(
        mode=CONTRACT,
        prompt_template=template,
        context=CONTEXT,
        text=text,
        profile=PROFILE,
        ollama_client=client,
        optimization_settings={MODE: OPT},
    )
    plan, _, _ = tool.plan_for(template, CONTEXT, text, PROFILE, OPT)
    assert [_document(r["prompt"]) for r in requests] == [call.text for call in plan.calls]


def test_prompt_size_changes_the_budget_but_frozen_input_is_identical_for_every_prompt():
    text = build_service_contract(100)
    _, budget_v2, _ = tool.plan_for(V2, CONTEXT, text, PROFILE, OPT)
    _, budget_v3, _ = tool.plan_for(V3, CONTEXT, text, PROFILE, OPT)
    assert budget_v3 > budget_v2  # v3 is smaller -> more room for text (end-to-end differs)

    frozen, _, _ = tool.plan_for(V2, CONTEXT, text, PROFILE, OPT)
    sent = {}
    for name, template in (("v2", V2), ("v3", V3), ("v3-output-first", tool.output_first(V3))):
        client, requests = _client()
        row = tool.run_frozen(template, CONTEXT, frozen, PROFILE, client)
        assert row["error"] is None and row["ai_calls"] == len(frozen.calls)
        assert row["prompt_eval_count"] == 100 * len(frozen.calls)
        sent[name] = [_document(r["prompt"]) for r in requests]
    assert sent["v2"] == sent["v3"] == sent["v3-output-first"] == [c.text for c in frozen.calls]


def test_probe_report_counts_raw_normalized_and_sent_and_shows_context_only_on_request():
    text = build_service_contract(60)
    plan, _, _ = tool.plan_for(V3, CONTEXT, text, PROFILE, OPT)
    sent = "\n\n".join(c.text for c in plan.calls)
    rows = {r["probe"]: r for r in tool.probe_report(["Kündigungsfrist beträgt 6 Monate"], text, text, sent)}
    assert rows["Kündigungsfrist beträgt 6 Monate"]["sent"] == 1
    assert "sent_context" not in rows["Kündigungsfrist beträgt 6 Monate"]
    with_context = tool.probe_report(["6 Monate"], text, text, sent, context_chars=20)[0]
    assert with_context["sent_context"] and "6 Monate" in with_context["sent_context"][0]


# --- Prompt variants and sources -------------------------------------------------------


def test_output_first_ablation_only_moves_the_output_section():
    ablated = tool.output_first(V3)
    assert sorted(ablated.splitlines()) == sorted(V3.splitlines()) or set(ablated.splitlines()) == set(
        V3.splitlines()
    )
    assert ablated.index("OUTPUT\n") < ablated.index("CONTEXT:\n") < ablated.index("DOCUMENT TEXT:\n")
    assert ablated.count("{text}") == 1 and ablated.rstrip().endswith("{text}")
    with pytest.raises(ValueError):
        tool.output_first(V2)  # v2 already has the instruction before the text


def test_prompt_source_reports_a_runtime_lab_version_shadowing_the_repo_file(tmp_path):
    runtime = tmp_path / "runtime"
    (runtime / MODE).mkdir(parents=True)
    (runtime / MODE / "v9.txt").write_text("LAB {context_block} {text}", encoding="utf-8")
    registry = PromptRegistry(PROMPTS_DIR, runtime, 32000)
    assert tool.resolve_prompt("v3", registry, runtime)[1] == "base (repo)"
    template, source = tool.resolve_prompt("v9", registry, runtime)
    assert source == "runtime (Lab-saved)" and template.startswith("LAB")
    draft = tmp_path / "draft.txt"
    draft.write_text("DRAFT {context_block} {text}", encoding="utf-8")
    assert tool.resolve_prompt(f"file:{draft}", registry, runtime)[0].startswith("DRAFT")
    assert "ablation" in tool.resolve_prompt("v3-output-first", registry, runtime)[1]


# --- Result explanation, gold check, schema audit ------------------------------------------


def test_amount_display_explains_7_00_from_a_thousands_separator_misread():
    assert tool.hubdix_amount_display(6.999) == "7,00"
    assert tool.hubdix_amount_display(6999.0) == "6.999,00"
    finding = tool.explain_result({"amount": 6.999, "payment_interval": "monthly"})["findings"]
    assert any("thousands separator" in f for f in finding)
    assert not tool.explain_result({"amount": 6999.0})["findings"]


def test_explain_flags_text_only_notice_and_renewal_without_period():
    findings = tool.explain_result(
        {
            "notice_period": "90 Tage vor Ablauf",
            "notice_period_value": None,
            "auto_renewal": True,
            "renewal_period_value": None,
            "renewal_terms": "automatisch",
        }  # fmt: skip
    )["findings"]
    assert any("no structured notice" in f for f in findings)
    assert any("only 'automatisch'" in f for f in findings)


def test_gold_check_supports_exact_contains_and_any_of():
    result = {
        "contract_number": None,
        "notice_period": "90 Tage vor Ablauf der Laufzeit",
        "payment_interval": "annual",
    }
    checks = tool.check_against_gold(
        result,
        {
            "contract_number": None,
            "notice_period": {"contains": ["90 Tage", "Ablauf"]},
            "payment_interval": {"any_of": ["annual"]},
            "amount": 6999.0,
        },
    )
    assert [checks[f]["ok"] for f in ("contract_number", "notice_period", "payment_interval", "amount")] == [
        True,
        True,
        True,
        False,
    ]
    assert tool.check_against_gold(None, {"amount": 1})["amount"]["ok"] is False


def test_schema_audit_is_deterministic():
    audit = tool.schema_audit(CONTRACT.response_schema)
    assert audit["field_count"] == audit["required_count"] == audit["nullable_count"] == 19
    assert set(audit["enum_fields"]) == {"notice_period_unit", "renewal_period_unit", "payment_interval"}
    assert audit["pattern_fields"] == ["start_date", "end_date", "currency"]
    assert audit["number_fields"] == ["amount"]
    assert audit["fields_with_description"] == [] and audit["nesting_depth"] == 1


# --- CLI: output only to stdout, no document text unless asked, nothing logged -----------------


@pytest.fixture
def settings(tmp_path, monkeypatch):
    base = _build_test_settings(tmp_path / "runtime-prompts")
    models = {name: dataclasses.replace(p, context_window_tokens=8192) for name, p in base.models.items()}
    configured = dataclasses.replace(base, models=models)
    monkeypatch.setattr(tool, "_settings", lambda: configured)
    return configured


def _contract_file(tmp_path) -> str:
    path = tmp_path / "contract.txt"
    text = build_service_contract(60).replace("Musterlift Care Servicevertrag", f"Musterlift Care {SECRET}")
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_cli_plan_reports_sizes_and_probes_without_document_text(tmp_path, settings, caplog):
    out = io.StringIO()
    profile = next(iter(settings.models))
    with caplog.at_level(logging.DEBUG):
        tool.main(
            ["plan", "--text", _contract_file(tmp_path), "--profile", profile, "--probe", SECRET], out=out
        )
    report = json.loads(out.getvalue())
    assert report["stats"]["strategy"] == "relevance" and report["prompt_source"] == "base (repo)"
    assert {r["probe"]: r["sent"] for r in report["probes"]}[SECRET] == 1
    assert SECRET not in json.dumps(report["stats"]) and "sent_text" not in report
    assert not any(SECRET in r.getMessage() for r in caplog.records)


def test_cli_plan_prints_the_sent_text_only_with_show_text(tmp_path, settings):
    out = io.StringIO()
    profile = next(iter(settings.models))
    tool.main(["plan", "--text", _contract_file(tmp_path), "--profile", profile, "--show-text"], out=out)
    sent = "\n".join(call["text"] for call in json.loads(out.getvalue())["sent_text"])
    for snippet in SERVICE_CORE_SNIPPETS[1:]:
        assert snippet in sent, snippet


def test_cli_run_matrix_end_to_end_and_frozen_with_gold_and_no_logging(
    tmp_path, settings, monkeypatch, caplog
):
    client, requests = _client(lambda payload: {**{f: None for f in CONTRACT.response_schema["properties"]},
                                                "amount": 6.999, "payment_interval": "monthly"})  # fmt: skip
    monkeypatch.setattr(tool, "_client", lambda s: client)
    gold = tmp_path / "gold.json"
    gold.write_text(json.dumps({"contract_number": None, "amount": 6999.0}), encoding="utf-8")
    out = io.StringIO()
    profile = next(iter(settings.models))
    with caplog.at_level(logging.DEBUG):
        tool.main(
            [
                "run",
                "--text",
                _contract_file(tmp_path),
                "--profile",
                profile,
                "--prompts",
                "v2,v3,v3-output-first",
                "--models",
                "model-a,model-b",
                "--gold",
                str(gold),
            ],  # fmt: skip
            out=out,
        )
    report = json.loads(out.getvalue())
    runs = report["runs"]
    assert len(runs) == 3 * 2 * 2  # prompts x models x (end-to-end, frozen)
    assert {r["model"] for r in runs} == {"model-a", "model-b"} and {r["input"] for r in runs} == {
        "end-to-end",
        "frozen",
    }
    assert all(r["gold_check"]["contract_number"]["ok"] and not r["gold_check"]["amount"]["ok"] for r in runs)
    assert all(r["explain"]["hubdix_amount_display"] == "7,00" for r in runs)
    assert {json.loads(json.dumps(r["prompt_eval_count"])) for r in runs} == {100}
    assert report["frozen_input"]["planned_with"] == "v2"
    assert {p["model"] for p in requests} == {"model-a", "model-b"}  # per-call model name only
    assert not any(SECRET in r.getMessage() for r in caplog.records)


def test_cli_schema_and_explain_need_no_config(tmp_path):
    out = io.StringIO()
    tool.main(["schema"], out=out)
    assert json.loads(out.getvalue())["field_count"] == 19
    result = tmp_path / "result.json"
    result.write_text(json.dumps({"amount": 6.999}), encoding="utf-8")
    out = io.StringIO()
    tool.main(["explain", "--result", str(result)], out=out)
    assert json.loads(out.getvalue())["hubdix_amount_display"] == "7,00"


def test_no_api_module_imports_the_diagnosis_tool():
    system_dir = PROMPTS_DIR.parent
    api_modules = [p for p in system_dir.rglob("*.py") if "tools" not in p.relative_to(system_dir).parts]
    offenders = [str(p) for p in api_modules if "system.tools" in p.read_text(encoding="utf-8")]
    assert offenders == []
