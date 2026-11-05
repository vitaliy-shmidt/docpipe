"""contract_extraction prompt v3: compact structure + field-scope rules.

Two layers, both deterministic:

1. The prompt itself: structure, field list = schema, every safety rule of
   v2 still present (and only once), the new scopes for contract_number,
   notice_period and renewal_terms, compactness, v1/v2 untouched.
2. Evidence delivery for the real-world disambiguation cases: through the
   REAL OllamaClient (mocked transport) and the real planner, the clause
   that belongs to a field AND the look-alike distractors (offer number,
   special termination, offer validity, payment term, sub-service) reach
   the model together with the rule that separates them.

What the model then answers is not testable without a model - that is the
staging quality gate (docs/contract-text-optimization.md "Staging
validation"). All texts are synthetic; no vendor-specific rules.
"""

from __future__ import annotations

import hashlib
import json
import re

import httpx
import pytest

from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.prompting import build_prompt
from system.ai.text_optimization.settings import TextOptimizationSettings
from system.config import ModelProfile
from system.services.ollama import OllamaClient
from tests.contract_samples import filler_section

MODE = "contract_extraction"
CONTRACT = MODES[MODE]
SCHEMA_FIELDS = list(CONTRACT.response_schema["properties"])


def _read(version: str) -> str:
    return (PROMPTS_DIR / MODE / f"{version}.txt").read_text(encoding="utf-8")


V2, V3 = _read("v2"), _read("v3")

# Shipped versions are immutable (Prompt Lab contract) - pinned by content
# (line endings normalized, so a CRLF checkout compares equal).
_PINNED = {
    "v1": "2b27efbed471a9900ea002046541e43943f2ba7d419218d34c62301ea5f18790",
    "v2": "08bfb1162b9bd117050369d0905f44a32e2684505e1a61b00755e795b639fd67",
}


# --- Versioning -------------------------------------------------------------------


def test_v3_is_the_default_and_v1_v2_are_unchanged():
    assert CONTRACT.default_prompt_version == "v3"
    for version, digest in _PINNED.items():
        content = (PROMPTS_DIR / MODE / f"{version}.txt").read_bytes().replace(b"\r\n", b"\n")
        assert hashlib.sha256(content).hexdigest() == digest, f"{version} must stay unchanged"


def test_v3_is_more_compact_than_v2():
    overhead = {
        v: len(build_prompt(t, {"document_type": "contract"}, "")) for v, t in (("v2", V2), ("v3", V3))
    }
    assert len(V3) < len(V2)
    assert overhead["v3"] <= overhead["v2"] * 0.95  # at least 5 % less prompt overhead per call


# --- Structure -----------------------------------------------------------------------


def test_v3_sections_are_in_order_and_output_follows_the_document():
    sections = ("ROLE\n", "INPUT\n", "GLOBAL RULES\n", "FIELDS (", "CONTEXT:\n", "DOCUMENT TEXT:\n")
    sections += ("OUTPUT\n",)
    positions = [V3.index(section) for section in sections]
    assert positions == sorted(positions)
    assert V3.count("{context_block}") == 1 and V3.count("{text}") == 1
    rendered = build_prompt(V3, {"document_type": "contract"}, "DOC-TEXT")
    assert rendered.index("DOC-TEXT") < rendered.index("Output ONLY the JSON object")


def test_v3_field_list_is_exactly_the_unchanged_schema():
    fields_section = V3.split("FIELDS (", 1)[1].split("\nCONTEXT:", 1)[0]
    listed = re.findall(r"^- ([a-z_]+):", fields_section, re.M)
    assert listed == SCHEMA_FIELDS
    assert "exactly these keys, no more and no fewer" in V3
    assert CONTRACT.response_schema["additionalProperties"] is False


# --- Safety semantics kept (each once) -------------------------------------------------


SAFETY_RULES = (
    "Use context ONLY to resolve genuine ambiguity",
    "Never use a context value to fill an output field unless the document text itself",
    "Only report facts explicitly stated in the document text",
    "A null is always better than a guess.",
    "Never invent a contract term",
    "Never compute a date from another date plus a duration.",
    "DD.MM.YYYY is day-month-year",
    "Do not give a legal interpretation.",
    "do not pick one or reconcile them",
    "also across different excerpts",
    "Never assume that the contract is currently active",
    "never convert between net and gross amounts",
    "only if the document states exactly one price for the contract",
    "Never output any identifier, database ID",
    "[Excerpt n/m]: all excerpts are parts of the SAME contract document",
    'Multiple spaces or "|" may separate the columns of a table.',
    "Output ONLY the JSON object",
)


@pytest.mark.parametrize("rule", SAFETY_RULES)
def test_v3_keeps_every_safety_rule_exactly_once(rule):
    assert V3.count(rule) == 1, rule


def test_v3_keeps_the_structured_fields_null_rules():
    for rule in (
        "null whenever the value is null",
        "null whenever renewal_period_value is null",
        "only if amount is not null",
        "null if amount is null",
        "only if notice_period is ONE plain duration",
        "only if the renewal period is ONE plain duration",
    ):
        assert rule in V3, rule


# --- New field scopes ------------------------------------------------------------------


def test_v3_contract_number_scope():
    rule = re.search(r"^- contract_number: (.+)$", V3, re.M).group(1)
    assert "only a value the document explicitly labels as the contract's number or ID" in rule
    assert "never construct one" in rule
    for excluded in ("document title", "product or package name", "tariff", "project name", "offer", "order",
                     "invoice", "customer", "installation/serial", "page number"):  # fmt: skip
        assert excluded in rule, excluded
    assert "If several identifiers are present, use only the one labelled as the contract number" in rule
    assert rule.endswith("if there is none, null.")


def test_v3_notice_scope():
    rule = re.search(r"^- notice_period: (.+)$", V3, re.M).group(1)
    assert "ORDINARY termination of the main contract at the end of its regular" in rule
    for excluded in ("extraordinary termination", "important reason", "immediate effect", "force majeure",
                     "payment default", "price changes", "objection to changed terms", "a sub-service",
                     "an offer's validity", "a payment term"):  # fmt: skip
        assert excluded in rule, excluded


def test_v3_renewal_scope_keeps_the_stated_duration():
    rule = re.search(r"^- renewal_terms: (.+)$", V3, re.M).group(1)
    assert "Keep any stated renewal duration and its condition" in rule
    assert 'never reduce it to just "automatisch"' in rule
    assert "of the main contract" in rule


def test_v3_main_contract_vs_sub_service_and_term_without_end_date():
    assert "Term, notice and renewal fields describe the MAIN contract." in V3
    assert "end_date is null and the duration goes into notes" in V3
    assert "Prices of single add-on services or line items are not the contract price." in V3


# --- Evidence delivery through the real client and planner --------------------------------

PROFILE_8K = ModelProfile(
    provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=8192
)


def _send(text: str) -> tuple[str, dict]:
    """Runs the real extraction path; returns (document text sent, result)."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        empty = {field: None for field in SCHEMA_FIELDS}
        return httpx.Response(200, json={"response": json.dumps(empty), "done": True})

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = run_extraction(
        mode=CONTRACT,
        prompt_template=_read(CONTRACT.default_prompt_version),
        context={},
        text=text,
        profile=PROFILE_8K,
        ollama_client=client,
        optimization_settings={MODE: TextOptimizationSettings()},
    )
    assert len(requests) == 1
    assert outcome.optimization["strategy"] == "relevance"  # selected by the planner, not sent whole
    prompt = requests[0]["prompt"]
    assert "ORDINARY termination of the main contract" in prompt  # the active rule travels along
    document = prompt.split("DOCUMENT TEXT:\n", 1)[1].split("\n\nOUTPUT\n", 1)[0]
    return document, outcome.result


def _long(head: str, clauses: list[str]) -> str:
    """A realistic long contract: head, clauses spread over neutral prose."""
    parts = [head]
    for i, clause in enumerate(clauses):
        parts += [filler_section(400 + i, 2500, dense=True), clause]
    parts += [filler_section(497 + i, 2500, dense=True) for i in range(3)]  # always long: relevance path
    return "\n\n".join(parts)


PARTIES = (
    "zwischen\nHotel Beispielhof GmbH (Auftraggeber)\nund\n"
    "ACME Aufzüge GmbH, Beispielweg 1, 12345 Musterstadt (Auftragnehmer)"
)


def test_title_project_and_offer_number_without_contract_number():
    head = f"Ihr ACME Care Smart Servicevertrag\n\nProjekt: PR-12345\nAngebot Nr.: A-998877\n\n{PARTIES}"
    document, result = _send(_long(head, ["§ 1 Vertragspartner\nVertragspartner ist die ACME Aufzüge GmbH."]))
    assert "Ihr ACME Care Smart Servicevertrag" in document and "Angebot Nr.: A-998877" in document
    assert not re.search(r"vertrags?[- ]?(nummer|nr)", document, re.I)  # nothing labelled as contract number
    assert result["contract_number"] is None


def test_explicit_contract_number_among_other_identifiers_reaches_the_model():
    head = (
        "Smart Servicevertrag\n\nAngebot Nr.: A-998877\nProjekt: PR-12345\nKundennummer: 4711\n"
        f"Vertragsnummer: V-2026-0042\n\n{PARTIES}"
    )
    document, _ = _send(_long(head, []))
    for line in ("Vertragsnummer: V-2026-0042", "Angebot Nr.: A-998877", "Projekt: PR-12345"):
        assert line in document, line


NOTICE_CASE = [
    "§ 3 Laufzeit und Kündigung\nDie Vertragslaufzeit beträgt 3 Jahre. Sofern der Vertrag nicht "
    "mindestens 90 Tage vor Ablauf der Vertragslaufzeit gekündigt wird, verlängert er sich automatisch.",
    "§ 9 Preisanpassung\nBei einer Preiserhöhung kann der Auftraggeber mit einer Frist "
    "von einem Monat kündigen.",
    "§ 12 Höhere Gewalt\nBei höherer Gewalt von mehr als 30 Tagen kann jede Partei "
    "mit sofortiger Wirkung kündigen.",
]


def test_ordinary_notice_and_special_termination_rights_all_reach_the_model():
    document, _ = _send(_long(PARTIES, NOTICE_CASE))
    assert "nicht mindestens 90 Tage vor Ablauf der Vertragslaufzeit gekündigt" in document
    # The distractors are sent too - the prompt rule separates them, the
    # planner does not hide them (they may belong into notes).
    assert "Frist von einem Monat kündigen" in document
    assert "30 Tagen kann jede Partei mit sofortiger Wirkung kündigen" in document


def test_renewal_with_explicit_duration_reaches_the_model_complete():
    clause = (
        "§ 4 Laufzeit\nDer Vertrag läuft zunächst 3 Jahre. Sofern nicht mindestens 90 Tage vor "
        "Ablauf gekündigt, verlängert er sich automatisch um die anfängliche Laufzeit oder 2 Jahre, "
        "je nachdem welcher Zeitraum länger ist."
    )
    document, _ = _send(_long(PARTIES, [clause]))
    assert "um die anfängliche Laufzeit oder 2 Jahre, je nachdem welcher Zeitraum länger ist" in document


def test_main_contract_and_sub_service_terms_both_reach_the_model():
    main = (
        "§ 2 Laufzeit\nDer Hauptvertrag hat eine Laufzeit von 3 Jahren. Die Kündigungsfrist "
        "beträgt 90 Tage zum Laufzeitende. Der Vertrag verlängert sich automatisch um 3 Jahre."
    )
    digital = (
        "§ 15 Digital Service\nDer Digital Service verlängert sich jeweils um 12 Monate und kann "
        "mit einer Frist von 30 Tagen gekündigt werden."
    )
    document, _ = _send(_long(PARTIES, [main, digital]))
    assert "Die Kündigungsfrist beträgt 90 Tage zum Laufzeitende." in document
    assert "Der Digital Service verlängert sich jeweils um 12 Monate" in document


def test_offer_validity_does_not_hide_term_and_notice():
    clauses = [
        "§ 1 Angebot\nAngebotsgültigkeit: 30 Tage.",
        "§ 5 Vertragslaufzeit\nVertragslaufzeit: 5 Jahre.",
        "§ 6 Kündigung\nKündigungsfrist: 6 Monate zum Laufzeitende.",
    ]
    document, _ = _send(_long(PARTIES, clauses))
    assert "Vertragslaufzeit: 5 Jahre." in document
    assert "Kündigungsfrist: 6 Monate zum Laufzeitende." in document


def test_payment_term_and_notice_both_reach_the_model():
    clauses = [
        "§ 7 Zahlung\nDie Rechnung ist zahlbar innerhalb 14 Tagen netto.",
        "§ 8 Kündigung\nDer Vertrag kann mit einer Frist von 3 Monaten zum Ende der "
        "Vertragslaufzeit gekündigt werden.",
    ]
    document, _ = _send(_long(PARTIES, clauses))
    assert "zahlbar innerhalb 14 Tagen" in document
    assert "mit einer Frist von 3 Monaten zum Ende der Vertragslaufzeit gekündigt" in document
