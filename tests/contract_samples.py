"""Synthetic contract texts for the text optimizer tests - invented
companies/people/numbers only, never real document content."""

from __future__ import annotations

HEADER = "Musterlift Aufzugsservice GmbH · Beispielweg 12 · 12345 Musterstadt"
FOOTER_TEMPLATE = "Wartungsvertrag WV-2026-0815 · Seite {page} von {pages}"

HEAD = (
    "WARTUNGSVERTRAG\n"
    "Vertragsnummer: WV-2026-0815\n\n"
    "zwischen\n"
    "Hotel Beispielhof GmbH, Am Markt 1, 54321 Beispielstadt\n"
    "- nachfolgend Auftraggeber genannt -\n\n"
    "und\n"
    "Musterlift Aufzugsservice GmbH, Beispielweg 12, 12345 Musterstadt\n"
    "- nachfolgend Auftragnehmer genannt -\n"
    "Ansprechpartner: Max Mustermann"
)

SUBJECT = (
    "§ 1 Vertragsgegenstand\n"
    "Gegenstand dieses Vertrages ist die regelmäßige Wartung und Inspektion der im "
    "Anhang aufgeführten Aufzugsanlagen des Auftraggebers durch den Auftragnehmer."
)

SERVICES = (
    "§ 2 Leistungsumfang\n"
    "Der Leistungsumfang umfasst die vierteljährliche Wartung nach Herstellervorgaben, "
    "die Prüfung der Sicherheitseinrichtungen sowie die Dokumentation im Anlagenbuch."
)

PRICE_TABLE = (
    "§ 3 Vergütung\n"
    "Leistung  |  Preis  |  Intervall\n"
    "Wartung Aufzug 1  |  450,00 €  |  monatlich\n"
    "Wartung Aufzug 2  |  450,00 €  |  monatlich\n"
    "Die Vergütung ist jeweils zum Monatsbeginn fällig."
)

TERM = (
    "§ 4 Laufzeit\n"
    "Der Vertrag beginnt am 01.01.2026 und wird zunächst für eine Laufzeit von 24 Monaten "
    "geschlossen."
)

RENEWAL = (
    "§ 5 Verlängerung\n"
    "Der Vertrag verlängert sich automatisch um jeweils weitere 12 Monate, sofern er nicht "
    "fristgerecht beendet wird."
)

NOTICE_AT_END = (
    "§ 14 Kündigung\nKündigungsfrist: 3 Monate zum Vertragsende. Die Kündigung bedarf der Schriftform."
)

SIGNATURES = "Musterstadt, den 15.11.2025\n\nAuftraggeber                Auftragnehmer"

# Neutral filler: no contract_extraction keywords at all.
_FILLER_SENTENCES = (
    "Die Parteien arbeiten vertrauensvoll zusammen und informieren sich gegenseitig "
    "über alle Umstände, die für die Durchführung von Bedeutung sein können",
    "Mündliche Nebenabreden bestehen nicht",
    "Sollte eine Bestimmung unwirksam sein, bleibt die Wirksamkeit der übrigen Bestimmungen unberührt",
    "Die Haftung richtet sich nach den gesetzlichen Vorschriften, soweit nachfolgend "
    "nichts anderes bestimmt ist",
    "Personenbezogene Daten werden ausschließlich im Rahmen der geltenden "
    "Datenschutzvorschriften verarbeitet",
)

# "Realistic" filler: ordinary clauses that still mention parties and
# services - the way real contracts use these words in almost every
# paragraph - so relevance selection has to rank, not just find.
_DENSE_FILLER_SENTENCES = (
    "Der Auftragnehmer erbringt seine Leistungen mit der Sorgfalt eines ordentlichen Kaufmanns",
    "Der Auftraggeber stellt dem Auftragnehmer die erforderlichen Zugänge zur Verfügung",
    "Mängel der Leistung sind dem Auftragnehmer unverzüglich anzuzeigen",
    "Der Auftragnehmer setzt ausschließlich geschultes Personal ein",
    "Die Haftung des Auftragnehmers für leichte Fahrlässigkeit ist ausgeschlossen, soweit "
    "gesetzlich zulässig",
    "Der Auftragnehmer unterhält eine Betriebshaftpflichtversicherung in angemessener Höhe",
)


def filler_section(number: int, chars: int, *, dense: bool = False) -> str:
    """Clause text without contract_extraction facts, varied per section
    (rotated sentences plus numbering) so no two sections are exact
    duplicates."""
    sentences = _DENSE_FILLER_SENTENCES if dense else _FILLER_SENTENCES
    parts: list[str] = []
    length = 0
    while length < chars:
        part = f"({number}.{len(parts) + 1}) {sentences[(number + len(parts)) % len(sentences)]}."
        parts.append(part)
        length += len(part) + 1
    return f"§ {number} Allgemeine Bestimmungen Teil {number}\n" + " ".join(parts)


def build_contract(
    target_chars: int, *, notice_at_end: bool = True, pages: int = 0, dense: bool = False
) -> str:
    """A German service contract of roughly `target_chars`: the relevant
    clauses at fixed places (renewal in the middle, notice period at the
    end), filler in between. With `pages`, page breaks (form feed) plus a
    repeated letterhead/footer are added."""
    core = [HEAD, SUBJECT, SERVICES, PRICE_TABLE, TERM]
    tail = [NOTICE_AT_END, SIGNATURES] if notice_at_end else [SIGNATURES]
    fixed = sum(len(part) + 2 for part in core + tail + [RENEWAL])
    remaining = max(0, target_chars - fixed)
    filler_count = max(2, remaining // 1500)
    fillers = [filler_section(6 + i, remaining // filler_count, dense=dense) for i in range(filler_count)]
    middle = fillers[: filler_count // 2] + [RENEWAL] + fillers[filler_count // 2 :]
    sections = core + middle + tail
    if not pages:
        return "\n\n".join(sections)
    per_page = max(1, len(sections) // pages)
    out = []
    for page in range(pages):
        end = (page + 1) * per_page if page < pages - 1 else None
        body = "\n\n".join(sections[page * per_page : end])
        out.append(f"{HEADER}\n\n{body}\n\n{FOOTER_TEMPLATE.format(page=page + 1, pages=pages)}")
    return "\f".join(out)


RELEVANT_SNIPPETS = (
    "WV-2026-0815",
    "Musterlift Aufzugsservice GmbH",
    "Leistungsumfang",
    "450,00 €",
    "01.01.2026",
    "verlängert sich automatisch",
    "Kündigungsfrist: 3 Monate",
)
