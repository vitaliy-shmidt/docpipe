"""Regression check for contract_extraction after text optimization: through
the REAL OllamaClient request path (only the HTTP transport is mocked), every
strategy (direct / relevance / chunked) must send document text inside a
complete prompt with the JSON schema, and the values the model returns must
survive validation and the chunk merge - never an empty/all-null result.

The fake model "reads" only the DOCUMENT TEXT it was sent, so a value can
only come back if the optimizer actually forwarded the relevant passage.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES
from system.ai.text_optimization.settings import default_text_optimization
from system.config import ModelProfile
from system.services.ollama import OllamaClient

CONTRACT = MODES["contract_extraction"]
TEMPLATE = open("system/prompts/contract_extraction/v1.txt", encoding="utf-8").read()
PROFILE = ModelProfile(provider="ollama", model="m", timeout_seconds=60, temperature=0)
EXPECTED = {
    "vendor_name": "Musterlift Testservice GmbH",
    "contract_number": "QA-2026-0815",
    "start_date": "2026-01-01",
    "notice_period_value": 3,
    "amount": 1200.0,
}

HEAD = (
    "WARTUNGSVERTRAG\nVertragsnummer: QA-2026-0815\n"
    "zwischen Testhaus Beispiel GmbH (Auftraggeber)\n"
    "und Musterlift Testservice GmbH (Auftragnehmer)\n\n"
    "§ 1 Vertragsgegenstand\nWartung der Aufzugsanlagen.\n\n"
)
KEY = (
    "§ 4 Laufzeit\nVertragsbeginn: 01.01.2026\n\n"
    "§ 5 Kündigung\nDie Kündigungsfrist beträgt 3 Monate zum Vertragsende.\n\n"
    "§ 6 Vergütung\nDie Vergütung beträgt 1.200,00 EUR monatlich zzgl. MwSt.\n\n"
)
FILLER = (
    "Der Auftragnehmer führt die vereinbarten Leistungen fachgerecht nach dem Stand "
    "der Technik aus und dokumentiert jeden Einsatz schriftlich. "
)


def _filler(count: int, tag: str) -> str:
    return "".join(f"§ {tag}{i} Allgemeines\n{FILLER * 3}\n\n" for i in range(count))


def _extra_fees(count: int) -> str:
    return "".join(
        f"§ X{i} Vergütung Zusatzleistung\nPauschale {i},00 EUR je Einsatz.\n\n" for i in range(count)
    )


CASES = {
    "direct": HEAD + KEY,
    "relevance": HEAD + _filler(14, "A") + KEY + _filler(14, "B"),
    "chunked": HEAD + _filler(40, "A") + KEY + _filler(40, "B") + _extra_fees(25) + _filler(30, "C"),
}


def _fake_model_answer(document: str) -> dict:
    result = {field: None for field in CONTRACT.response_schema["properties"]}
    if m := re.search(r"und (.+?) \(Auftragnehmer\)", document):
        result["vendor_name"] = m.group(1)
    if m := re.search(r"Vertragsnummer:\s*([A-Z0-9-]+)", document):
        result["contract_number"] = m.group(1)
    if m := re.search(r"Vertragsbeginn:\s*(\d{2})\.(\d{2})\.(\d{4})", document):
        result["start_date"] = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if m := re.search(r"Kündigungsfrist beträgt (\d+) Monate", document):
        result["notice_period"] = m.group(0)
        result["notice_period_value"], result["notice_period_unit"] = int(m.group(1)), "months"
    if m := re.search(r"([\d.]+,\d{2})\s*EUR\s+monatlich", document):
        result["amount"] = float(m.group(1).replace(".", "").replace(",", "."))
        result["currency"], result["payment_interval"] = "EUR", "monthly"
    return result


@pytest.mark.parametrize("strategy", ["direct", "relevance", "chunked"])
def test_every_strategy_sends_text_and_keeps_model_values(strategy):
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        document = payload["prompt"].split("DOCUMENT TEXT:\n", 1)[1]
        return httpx.Response(200, json={"response": json.dumps(_fake_model_answer(document)), "done": True})

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))

    outcome = run_extraction(
        mode=CONTRACT,
        prompt_template=TEMPLATE,
        context={},
        text=CASES[strategy],
        profile=PROFILE,
        ollama_client=client,
        optimization_settings=default_text_optimization(),
    )

    stats = outcome.optimization
    assert stats["strategy"] == strategy
    assert stats["ai_calls"] == len(requests) >= 1
    assert 0 < stats["optimized_chars"] <= stats["raw_chars"]
    for payload in requests:
        document = payload["prompt"].split("DOCUMENT TEXT:\n", 1)[1]
        assert document.strip(), "a call was sent without document text"
        assert payload["prompt"].startswith(TEMPLATE.split("{context_block}", 1)[0])
        assert payload["format"] == CONTRACT.response_schema
    for field, value in EXPECTED.items():
        assert outcome.result[field] == value, field
    assert sum(value is not None for value in outcome.result.values()) >= len(EXPECTED)
