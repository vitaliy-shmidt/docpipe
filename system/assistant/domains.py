"""Assistant domains: which data area(s) a conversation is about.

A consumer (HubDix) knows which page the user is on and which data areas
("domains") it prepared context for. It may tell DocPipe those domain
NAMES - a task hint, exactly like an extraction `mode` - never a model,
profile or prompt. DocPipe turns `domains` (+ the question text) into an
assistant mode; system/ai/resolver.py turns the mode into a model.

    domains + question -> resolve_assistant_route() -> AssistantMode -> profile -> model

Rules (deterministic, testable, no LLM router):

- no domains            -> legacy question-only routing (system/assistant/router.py),
                           unchanged behavior for existing callers
- ["hotel_health"]      -> legacy routing too: the hotel overview context
                           carries all areas, so keyword routing is still right
- one focused domain    -> that domain's mode; a document-intent question
                           inside a domain that allows it goes to
                           document_question (e.g. "Was steht im Vertrag zur
                           Haftung?" in contracts)
- a domain without a dedicated mode (projects, defects), or several
  domains at once      -> cross_domain_question

The domain list is capped (MAX_DOMAINS) as a payload/abuse guard only - the
real product limit for a small local model lives in the consumer (HubDix
ASSISTANT_MAX_ACTIVE_DOMAINS) and can be raised there without a DocPipe
change; a later "global" assistant only needs this cap raised.
"""

from __future__ import annotations

from dataclasses import dataclass

from system.assistant.router import PRIORITY_RULES, AssistantRoute, route
from system.errors import DocPipeError

CROSS_DOMAIN_MODE = "cross_domain_question"
MAX_DOMAINS = 4


@dataclass(frozen=True)
class AssistantDomain:
    name: str
    # Mode for a focused question in this domain (None = legacy routing).
    default_mode: str | None
    # A document-intent question may be answered by document_question.
    allows_document_mode: bool = False


ASSISTANT_DOMAINS: dict[str, AssistantDomain] = {
    "hotel_health": AssistantDomain("hotel_health", None),
    "contracts": AssistantDomain("contracts", "contract_question", allows_document_mode=True),
    "maintenance": AssistantDomain("maintenance", "maintenance_question", allows_document_mode=True),
    "inspections": AssistantDomain("inspections", "inspection_question", allows_document_mode=True),
    "documents": AssistantDomain("documents", "document_question"),
    "projects": AssistantDomain("projects", CROSS_DOMAIN_MODE),
    "defects": AssistantDomain("defects", CROSS_DOMAIN_MODE),
}

_DOCUMENT_KEYWORDS: tuple[str, ...] = next(
    kw for name, _mode, kw in PRIORITY_RULES if name == "document_keywords"
)


def validate_domains(domains: list[str]) -> list[str]:
    """Unique, known, capped. Raises DocPipeError("invalid_request")."""
    cleaned: list[str] = []
    for name in domains:
        if name not in ASSISTANT_DOMAINS:
            raise DocPipeError("invalid_request", f"Unknown assistant domain: {name!r}.")
        if name not in cleaned:
            cleaned.append(name)
    if len(cleaned) > MAX_DOMAINS:
        raise DocPipeError("invalid_request", f"At most {MAX_DOMAINS} assistant domains are allowed.")
    return cleaned


def resolve_assistant_route(question: str, domains: list[str]) -> AssistantRoute:
    if not domains or domains == ["hotel_health"]:
        return route(question)
    if len(domains) > 1:
        return AssistantRoute(mode=CROSS_DOMAIN_MODE, matched_rule="domains:" + "+".join(domains))
    domain = ASSISTANT_DOMAINS[domains[0]]
    normalized = question.lower()
    if domain.allows_document_mode and any(keyword in normalized for keyword in _DOCUMENT_KEYWORDS):
        return AssistantRoute(
            mode="document_question", matched_rule=f"domain:{domain.name}+document_keywords"
        )
    return AssistantRoute(mode=domain.default_mode or CROSS_DOMAIN_MODE, matched_rule=f"domain:{domain.name}")


def warmup_mode_for_domains(domains: list[str], fallback: str) -> str:
    """The mode a conversation in these domains most likely runs first."""
    if not domains or domains == ["hotel_health"]:
        return fallback
    if len(domains) > 1:
        return CROSS_DOMAIN_MODE
    return ASSISTANT_DOMAINS[domains[0]].default_mode or CROSS_DOMAIN_MODE
