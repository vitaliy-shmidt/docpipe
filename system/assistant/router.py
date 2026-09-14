"""Deterministic question -> assistant mode routing.

V1 is bluntly rule-based (keyword containment), on purpose - see the task:
"V1 bewusst deterministisch/regelbasiert. Nicht sofort LLM-Router bauen."
A rule-based router is trivially testable, has zero latency/cost of its
own, and fails in an obvious, debuggable way (a wrong match is a keyword
list to fix, not an opaque model decision).

The router does exactly one thing: `question -> AssistantMode name`. It
never reads a database, never calls HubDix, never fetches or searches a
document - it only classifies the text it was given. See
system/routes/assistant.py for what happens with the result.

Rule order matters (see PRIORITY_RULES below): a document-intent keyword
("Bericht"/"report") is checked *before* any domain-specific keyword, so
e.g. "Was steht im letzten Wartungsbericht?" ("Wartungsbericht" contains
both a maintenance word and a document word) resolves to
`document_question`, not `maintenance_question` - the question is about a
specific document's content, not maintenance data in general.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODE = "general_hotel_question"


@dataclass(frozen=True)
class AssistantRoute:
    mode: str
    # Which keyword rule matched (or "fallback") - for logging/debugging
    # only. Deliberately NOT a confidence score: there is no real
    # probabilistic signal behind a keyword match, so none is invented
    # (see the task: "Wenn keine echte probabilistische Confidence
    # vorhanden ist: nicht ausgeben").
    matched_rule: str


# Ordered highest-priority first. Each entry: (rule_name, mode, keywords).
# Keywords are plain lowercase substrings, checked against the lowercased
# question - intentionally simple, not word-boundary/regex matching (see
# module docstring: this is meant to stay a short, readable, testable
# list, not a tokenizer).
PRIORITY_RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "document_keywords",
        "document_question",
        (
            # DE
            "dokument", "bericht", "rechnung", "beleg", "protokoll", "urkunde",
            # EN
            "document", "report", "invoice", "receipt", "record",
        ),
    ),
    (
        "contract_keywords",
        "contract_question",
        (
            # DE
            "vertrag", "verträge", "vertraege", "kündigung", "kuendigung", "laufzeit",
            # EN
            "contract", "termination", "expir",  # "expir" covers expire/expires/expiring/expiration
        ),
    ),
    (
        "inspection_keywords",
        "inspection_question",
        (
            # DE
            "prüfung", "pruefung", "tüv", "tuev", "inspektion", "zertifikat",
            # EN
            "inspection", "certificate", "certification",
        ),
    ),
    (
        "maintenance_keywords",
        "maintenance_question",
        (
            # DE
            "wartung", "instandhaltung",
            # EN/DE shared
            "service", "maintenance",
        ),
    ),
    (
        "hotel_health_keywords",
        "hotel_health_summary",
        (
            # DE
            "technischer zustand", "technischen zustand", "technisch da",
            "technisch aufgestellt", "gesamtzustand", "hotel gesund", "kritisch",
            # EN
            "technical state", "technical condition", "hotel health", "how healthy", "critical issues",
        ),
    ),
]


def route(question: str) -> AssistantRoute:
    normalized = question.lower()
    for rule_name, mode, keywords in PRIORITY_RULES:
        if any(keyword in normalized for keyword in keywords):
            return AssistantRoute(mode=mode, matched_rule=rule_name)
    return AssistantRoute(mode=DEFAULT_MODE, matched_rule="fallback")
