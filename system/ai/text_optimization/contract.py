"""contract_extraction task knowledge for the text optimizer.

The field groups are the contract_extraction response schema
(system/prompts/contract_extraction/schema.json) partitioned by topic -
`notes` belongs to no group because any passage can feed it. A test
(tests/test_text_optimization.py) asserts these groups cover exactly the
schema's properties, so a schema change cannot silently drift away from
them. The patterns only decide which verbatim passages an AI call gets to
see; they never extract, convert, or interpret a value themselves.

Strong patterns name the clause itself ("Kündigungsfrist", "Vergütung", an
amount, "Vertragsnummer") - a block matching one is a passage that must
reach the model. Weak patterns are words real contracts use in nearly
every paragraph ("Auftragnehmer", "Leistungen", "monatlich", a bare date) -
they only rank blocks, they never force chunking on their own.

German stems are matched as substrings on purpose (compounds:
"Vertragslaufzeit", "Kündigungsfrist"); short or ambiguous English words
use word boundaries ("term" must not match "determine"). DE + EN only.
"""

from __future__ import annotations

import re

from system.ai.text_optimization.passages import KeywordGroup


def _patterns(*sources: str) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(source, re.IGNORECASE) for source in sources)


# Amounts in the usual spellings: "450,00 €", "1.200,00 EUR", "EUR 450",
# "€ 450", "$1,200.00", "450 Euro".
_AMOUNT = r"(?:\d[\d.,']*\s?(?:€|eur\b|euro\b|chf\b|usd\b|\$)|(?:€|eur\b|chf\b|usd\b|\$)\s?\d)"
# Dates: 01.01.2026, 1.1.26, 2026-01-01, 1. Januar 2026, January 1, 2026.
_MONTHS = (
    r"(?:januar|februar|märz|maerz|april|mai|juni|juli|august|september|oktober|november|dezember"
    r"|january|february|march|may|june|july|october|december)"
)
_DATE = (
    r"(?:\b\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})\b|\b\d{4}-\d{2}-\d{2}\b"
    rf"|\b\d{{1,2}}\.?\s+{_MONTHS}\s+\d{{4}}|\b{_MONTHS}\s+\d{{1,2}},?\s+\d{{4}})"
)

BASIS = KeywordGroup(
    name="basis",
    fields=("vendor_name", "contact_person", "contract_type", "contract_number", "service_description"),
    strong=_patterns(
        r"vertrags?[- ]?(?:nummer|nr\b)",
        r"kunden[- ]?(?:nummer|nr\b)",
        r"versicherungs(?:schein)?[- ]?(?:nummer|nr\b)",
        r"vertragspartner",
        r"vertragsgegenstand",
        r"leistungs(?:umfang|beschreibung|gegenstand|verzeichnis)",
        r"ansprechpartner",
        r"\b(?:contract|agreement|customer) (?:no|number)\b",
        r"\bscope of (?:services|work)\b",
        r"\bsubject matter\b",
        r"\bcontact person\b",
    ),
    weak=_patterns(
        r"auftragnehmer",
        r"auftraggeber",
        r"dienstleister",
        r"lieferant",
        r"anbieter",
        r"gegenstand",
        r"\bleistung(?:en)?\b",
        r"pflichten",
        r"wartung",
        r"\bzwischen\b",
        r"\bagreement\b",
        r"\bcontractor\b",
        r"\bsupplier\b",
        r"\bprovider\b",
        r"\bcustomer\b",
        r"\bservices?\b",
        r"\bmaintenance\b",
        r"\bbetween\b",
    ),
    carries_head=True,
)

TERM = KeywordGroup(
    name="term",
    fields=(
        "start_date",
        "end_date",
        "is_open_ended",
        "notice_period",
        "notice_period_value",
        "notice_period_unit",
        "renewal_terms",
        "auto_renewal",
        "renewal_period_value",
        "renewal_period_unit",
    ),
    strong=_patterns(
        r"vertragsbeginn",
        r"laufzeit",
        r"vertragsdauer",
        r"vertragsende",
        r"befristet",
        r"unbestimmte zeit",
        r"in kraft",
        r"kündig",
        r"verläng",
        r"stillschweigend",
        r"\bbeginnt\b",
        r"\bendet\b",
        r"\bduration\b",
        r"\bcommence",
        r"\beffective date\b",
        r"\bstart date\b",
        r"\bend date\b",
        r"\bexpir",
        r"\bterminat",
        r"\bnotice period\b",
        r"\brenew",
        r"\bindefinite",
    ),
    weak=_patterns(
        r"beginn",
        r"\bdauer\b",
        r"\bende\b",
        r"frist",
        r"erneuer",
        r"\bterm\b",
        r"\bnotice\b",
        r"\bextend",
        r"\bextension\b",
        _DATE,
    ),
)

FINANCE = KeywordGroup(
    name="finance",
    fields=("amount", "currency", "payment_interval"),
    strong=_patterns(
        r"preis",
        r"vergütung",
        r"entgelt",
        r"gebühr",
        r"honorar",
        r"\bprice\b",
        r"\bfees?\b",
        _AMOUNT,
    ),
    weak=_patterns(
        r"kosten",
        r"beitrag",
        r"pauschal",
        r"zahlung",
        r"fällig",
        r"rechnung",
        r"monatlich",
        r"jährlich",
        r"quartal",
        r"halbjähr",
        r"pro (?:monat|jahr|quartal)",
        r"netto",
        r"brutto",
        r"mwst",
        r"umsatzsteuer",
        r"\bpayment",
        r"\binvoice",
        r"\bmonthly\b",
        r"\bannual",
        r"\bquarterly\b",
        r"\bper (?:month|year|annum|quarter)\b",
        r"\bcosts?\b",
        r"\bcharges?\b",
        r"\bvat\b",
    ),
)

CONTRACT_GROUPS = (BASIS, TERM, FINANCE)

# Deterministic consistency rules for a merged chunked result, mirroring
# the prompt's own rules ("unit null whenever value null", "currency only
# if amount", "payment_interval null if amount is null"): dependent field
# -> the field it depends on. Applying them can only ever null a value.
CONTRACT_DEPENDENT_FIELDS = {
    "notice_period_unit": "notice_period_value",
    "notice_period_value": "notice_period_unit",
    "renewal_period_unit": "renewal_period_value",
    "renewal_period_value": "renewal_period_unit",
    "currency": "amount",
    "payment_interval": "amount",
}
