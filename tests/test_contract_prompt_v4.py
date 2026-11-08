"""contract_extraction prompt + schema v4: customer / site / asset facts.

v4 is additive: the 19 v3 fields keep their wording and semantics, four
document facts are added for the caller's own matching (customer_name,
customer_address, sites[], assets[]). DocPipe extracts facts only - it
never decides an application ID or an assignment.

Deterministic layers (what the model then answers is the staging gate,
docs/contract-text-optimization.md "Prompt v4"):

1. Versioning: v1-v3 prompts and the base schema are byte-identical, v3
   keeps the v3 schema, planner groups and output reserve (rollback = one
   Lab activation, also for the structured-output format).
2. The v4 schema (strict, nested, limits = HubDix sanitizer) and prompt
   (field list = schema, every v3 safety rule kept, the new conservative
   rules).
3. Planner: v4 groups cover the v4 schema; labelled customer / site /
   serial-number passages are mandatory, generic words only rank.
4. Merge: list union with dedupe + caps, customer conflicts -> null.
5. Evidence delivery and the v3/v4 input comparison through the REAL
   OllamaClient (mocked transport) and planner.
All texts are synthetic.
"""

from __future__ import annotations

import hashlib
import json
import re

import httpx
import jsonschema
import pytest

from system.ai.extraction_runner import run_extraction
from system.ai.modes import MODES, PROMPTS_DIR
from system.ai.prompting import build_prompt
from system.ai.text_optimization.contract import (
    BASIS,
    BASIS_V4,
    CONTRACT_GROUPS,
    CONTRACT_GROUPS_V4,
    CONTRACT_LIST_FIELDS_V4,
    SITE,
)
from system.ai.text_optimization.merge import merge_results
from system.ai.text_optimization.planner import OUTPUT_RESERVE_TOKENS
from system.ai.text_optimization.settings import (
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    OPTIMIZABLE_MODES,
    TextOptimizationSettings,
)
from system.config import ModelProfile
from system.services.ollama import OllamaClient
from tests.contract_samples import (
    PDF_RELEVANT_SNIPPETS,
    RELEVANT_SNIPPETS,
    SERVICE_CORE_SNIPPETS,
    build_contract,
    build_pdf_contract,
    build_service_contract,
    filler_section,
)

MODE = "contract_extraction"
CONTRACT = MODES[MODE]
V3_SCHEMA = CONTRACT.schema_for("v3")
V4_SCHEMA = CONTRACT.schema_for("v4")
NEW_FIELDS = ["customer_name", "customer_address", "sites", "assets"]
OPT = OPTIMIZABLE_MODES[MODE]


def _read(version: str) -> str:
    return (PROMPTS_DIR / MODE / f"{version}.txt").read_text(encoding="utf-8")


V3, V4 = _read("v3"), _read("v4")


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


# --- 1. Versioning -----------------------------------------------------------------


_PINNED = {
    "v1.txt": "2b27efbed471a9900ea002046541e43943f2ba7d419218d34c62301ea5f18790",
    "v2.txt": "08bfb1162b9bd117050369d0905f44a32e2684505e1a61b00755e795b639fd67",
    "v3.txt": "be53fe3963421e744e03c3570b9c6cdc154da116702a32a3e592dc520473ee5c",
    "schema.json": "379792061f6933aeb89e7070b470a97ebdfafb9532c083a0a6390d6b05ed4274",
}


@pytest.mark.parametrize("name", sorted(_PINNED))
def test_v1_to_v3_and_the_base_schema_are_unchanged(name):
    assert _digest(PROMPTS_DIR / MODE / name) == _PINNED[name], f"{name} must stay unchanged"


def test_v3_stays_the_default_until_v4_is_activated():
    # Activation after the staging comparison is one Prompt Lab call - the
    # code default only moves once that comparison is done.
    assert CONTRACT.default_prompt_version == "v3"


def test_schema_selection_per_version():
    for version in ("v1", "v2", "v3", None, "draft", "v03"):
        assert CONTRACT.schema_for(version) is CONTRACT.response_schema
    assert CONTRACT.schema_for("v4") is V4_SCHEMA and CONTRACT.schema_for("v9") is V4_SCHEMA
    assert V4_SCHEMA is not CONTRACT.response_schema
    # Other modes have no versioned schema.
    for name, mode in MODES.items():
        if name != MODE:
            assert mode.schema_versions == () and mode.schema_for("v4") is mode.response_schema


def test_planner_variant_per_version():
    for version in ("v1", "v3", None):
        variant = OPT.for_version(version)
        assert variant is OPT
        assert variant.groups == CONTRACT_GROUPS and variant.list_fields == {}
        assert variant.output_reserve_tokens == OUTPUT_RESERVE_TOKENS == DEFAULT_OUTPUT_RESERVE_TOKENS
    v4 = OPT.for_version("v4")
    assert v4.groups == CONTRACT_GROUPS_V4 and set(v4.list_fields) == {"sites", "assets"}
    assert OPT.for_version("v7") is v4
    # The v3 BASIS group itself is untouched.
    assert "customer_name" not in BASIS.fields and CONTRACT_GROUPS[0] is BASIS


# --- 2. Schema ---------------------------------------------------------------------


def test_v4_schema_is_v3_plus_four_fields_before_notes():
    v3_fields = list(V3_SCHEMA["properties"])
    v4_fields = list(V4_SCHEMA["properties"])
    assert v4_fields == v3_fields[:-1] + NEW_FIELDS + ["notes"]
    for name in v3_fields:
        assert V4_SCHEMA["properties"][name] == V3_SCHEMA["properties"][name], name
    assert V4_SCHEMA["required"] == v4_fields
    assert V4_SCHEMA["additionalProperties"] is False


def _objects(schema):
    """Every object schema inside `schema` (nested)."""
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            yield schema
        for value in schema.values():
            yield from _objects(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _objects(value)


def test_v4_schema_nested_objects_are_strict():
    objects = list(_objects(V4_SCHEMA))
    assert len(objects) == 4  # root, customer_address, site, asset
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert obj["required"] == list(obj["properties"])


# Same limits as HubDix's ContractImportService sanitizer.
LIMITS = {
    ("customer_name",): 250,
    ("customer_address", "street"): 150,
    ("customer_address", "zip"): 20,
    ("customer_address", "city"): 100,
    ("sites", "name"): 250,
    ("sites", "street"): 150,
    ("sites", "zip"): 20,
    ("sites", "city"): 100,
    ("assets", "name"): 250,
    ("assets", "serial_number"): 100,
    ("assets", "location"): 250,
}


def _sub(path):
    node = V4_SCHEMA["properties"][path[0]]
    if len(path) == 1:
        return node
    if "anyOf" in node:
        node = next(option for option in node["anyOf"] if option.get("type") == "object")
    if node.get("type") == "array":
        node = node["items"]
    return node["properties"][path[1]]


@pytest.mark.parametrize("path", sorted(LIMITS))
def test_v4_string_limits_match_the_consumer(path):
    assert _sub(path) == {"type": ["string", "null"], "maxLength": LIMITS[path]}


def test_v4_list_limits_and_types():
    assert V4_SCHEMA["properties"]["sites"]["maxItems"] == 10 == CONTRACT_LIST_FIELDS_V4["sites"].max_items
    assert V4_SCHEMA["properties"]["assets"]["maxItems"] == 50 == CONTRACT_LIST_FIELDS_V4["assets"].max_items
    assert V4_SCHEMA["properties"]["sites"]["type"] == "array"  # never null: [] when empty
    assert V4_SCHEMA["properties"]["assets"]["type"] == "array"


def test_v4_schema_has_no_application_ids():
    dumped = json.dumps(V4_SCHEMA)
    for forbidden in ("_id", "_ID", "hotel", "vendor_id", "legal_entit", "operator"):
        assert forbidden not in dumped, forbidden


def _result(**extra):
    base = {name: None for name in V4_SCHEMA["properties"]}
    base.update({"sites": [], "assets": []})
    base.update(extra)
    return base


SITE_A = {
    "name": "Pflegezentrum Waldmössingen",
    "street": "Heimbachstraße 8",
    "zip": "78713",
    "city": "Schramberg-Waldmössingen",
}
ASSET_A = {"name": "Aufzug 2", "serial_number": "FN-123 456", "location": "Haus A"}


def test_v4_schema_accepts_valid_results():
    jsonschema.validate(_result(), V4_SCHEMA)
    jsonschema.validate(
        _result(
            customer_name="Stegwiesen Pflegezentrum GmbH",
            customer_address={"street": "Stegwiesen 13", "zip": "78333", "city": "Stockach"},
            sites=[SITE_A],
            assets=[ASSET_A, {"name": "RLT Küche", "serial_number": None, "location": None}],
        ),
        V4_SCHEMA,
    )
    jsonschema.validate(
        _result(customer_address={"street": None, "zip": "78333", "city": "Stockach"}), V4_SCHEMA
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"sites": None},
        {"assets": None},
        {"customer_address": ["Stegwiesen 13"]},
        {"customer_address": {"street": "x", "zip": "1", "city": "y", "hotel_id": 5}},
        {"customer_address": {"street": "x"}},
        {"sites": [dict(SITE_A, hotels_ID=4)]},
        {"sites": [SITE_A] * 11},
        {"assets": [ASSET_A] * 51},
        {"assets": [dict(ASSET_A, serial_number="9" * 101)]},
        {"assets": [{"name": "Aufzug", "serial_number": 4711, "location": None}]},
        {"customer_name": "x" * 251},
    ],
)
def test_v4_schema_rejects_invalid_results(patch):
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_result(**patch), V4_SCHEMA)


def test_v4_schema_rejects_a_v3_shaped_result():
    v3_shaped = {name: None for name in V3_SCHEMA["properties"]}
    jsonschema.validate(v3_shaped, V3_SCHEMA)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(v3_shaped, V4_SCHEMA)


# --- 2b. Prompt ----------------------------------------------------------------------


def test_v4_structure_is_v3s():
    sections = (
        "ROLE\n",
        "INPUT\n",
        "GLOBAL RULES\n",
        "FIELDS (",
        "CONTEXT:\n",
        "DOCUMENT TEXT:\n",
        "OUTPUT\n",
    )
    assert [V4.index(s) for s in sections] == sorted(V4.index(s) for s in sections)
    assert V4.count("{context_block}") == 1 and V4.count("{text}") == 1
    build_prompt(V4, {"document_type": "contract"}, "DOC")


def test_v4_field_list_is_exactly_the_v4_schema():
    fields_section = V4.split("FIELDS (", 1)[1].split("\nCONTEXT:", 1)[0]
    assert re.findall(r"^- ([a-z_]+):", fields_section, re.M) == list(V4_SCHEMA["properties"])
    assert "exactly these keys, no more and no fewer" in V4


def test_v4_keeps_every_v3_line_verbatim():
    """v4 = v3 + additions: every v3 line (all field descriptions and safety
    rules) is still there unchanged, except the FIELDS heading that now also
    names the [] default for lists."""
    v4_lines = V4.splitlines()
    for line in V3.splitlines():
        if line.startswith("FIELDS ("):
            continue
        assert line in v4_lines, line


def test_v4_keeps_the_v3_safety_rules_exactly_once():
    from tests.test_contract_prompt_v3 import SAFETY_RULES

    for rule in SAFETY_RULES:
        assert V4.count(rule) == 1, rule


def _field(name: str) -> str:
    return re.search(rf"^- {name}: (.+)$", V4, re.M).group(1)


def test_v4_no_guessing_rules():
    for rule in (
        "null or [] is better than a guess",
        "Report only roles the document states",
        "never take a name, role or address from CONTEXT",
        "never derive a site from a company, letterhead, billing or postal address",
        "never create an asset from a generic word alone",
        "never classify a number by its format when it has no label",
    ):
        assert V4.count(rule) == 1, rule
    assert "Never output any identifier, database ID" in V4  # unchanged v3 rule covers the new fields too


def test_v4_customer_rules():
    rule = _field("customer_name")
    for label in ("Auftraggeber", "Kunde", "Besteller", "Mandant", "customer", "client"):
        assert label in rule, label
    assert "never the provider" in rule and "null if unclear" in rule
    assert "operator" not in V4.lower() and "betreiber" not in V4.lower()  # no invented role
    address = _field("customer_address")
    assert 'an object with "street", "zip", "city", missing parts null' in address
    assert "Not automatically the place of service" in address


def test_v4_site_rules():
    rule = _field("sites")
    for label in (
        "Standort",
        "Leistungsort",
        "Objekt",
        "Liegenschaft",
        "Anlagenstandort",
        "Aufstellungsort",
        "Betriebsstätte",
        "site",
        "premises",
    ):
        assert label in rule, label
    assert "explicitly labelled" in rule and "Never the provider's address" in rule
    assert "at most 10" in rule and "Each once" in rule


def test_v4_asset_and_serial_number_rules():
    rule = _field("assets")
    assert 'a generic word alone ("Aufzug", "RLT", "Heizung") is not one' in rule
    for label in (
        "Seriennummer",
        "Ser.-Nr.",
        "Fabriknummer",
        "Anlagen-Nr.",
        "Gerätenummer",
        "serial number",
        "S/N",
    ):
        assert label in rule, label
    assert "copied character by character" in rule
    assert "never an offer, contract, order, customer or invoice number" in rule
    assert "at most 50" in rule


def test_v4_overhead_growth_is_bounded():
    overhead = {
        v: len(build_prompt(t, {"document_type": "contract"}, "")) for v, t in (("v3", V3), ("v4", V4))
    }
    # Four new fields + one rule: at most ~1,600 chars (~500 tokens) more per call.
    assert 0 < overhead["v4"] - overhead["v3"] <= 1600, overhead


# --- 3. Planner groups -------------------------------------------------------------


def test_v4_groups_cover_exactly_the_v4_schema_except_notes():
    grouped = [field for group in CONTRACT_GROUPS_V4 for field in group.fields]
    assert sorted(grouped) == sorted(f for f in V4_SCHEMA["properties"] if f != "notes")
    assert len(grouped) == len(set(grouped))
    assert BASIS_V4.carries_head and not SITE.carries_head
    assert BASIS_V4.fields[: len(BASIS.fields)] == BASIS.fields
    assert set(BASIS.strong) <= set(BASIS_V4.strong) and set(BASIS.weak) <= set(BASIS_V4.weak)


def _strong(group, text) -> bool:
    return any(p.search(text) for p in group.strong)


@pytest.mark.parametrize(
    "line",
    [
        "Leistungsort: Heimbachstraße 8, 78713 Schramberg",
        "Anlagenstandort:",
        "Aufstellungsort der Anlage: Keller Haus B",
        "Liegenschaft Am Markt 1",
        "Betriebsstätte Nord",
        "Standort: Pflegezentrum Waldmössingen",
        "Objektanschrift: Seeweg 9",
        "Objekt: Haus am See",
        "Seriennummer 12345",
        "Ser.-Nr. 4711",
        "Fabriknummer: FN-1",
        "Fabr.-Nr. 99",
        "Anlagen-Nr. A-7",
        "Gerätenummer 5",
        "Serial No. X1",
        "S/N 123",
        "the premises at 1 Main Street",
    ],
)
def test_site_group_strong_patterns(line):
    assert _strong(SITE, line), line


@pytest.mark.parametrize(
    "line",
    [
        "Die Aufzugsanlage wird gewartet.",
        "Siehe Anlage 3 (Preisblatt).",
        "Der Standort der Verwaltung ist unerheblich.",
        "Vertragsnummer: V-1",
        "Angebot Nr. A-998877",
        "Kundennummer 4711",
    ],
)
def test_generic_words_and_other_numbers_are_not_strong_site_hits(line):
    assert not _strong(SITE, line), line


@pytest.mark.parametrize(
    "line",
    [
        "Auftraggeber: Stegwiesen GmbH",
        "- nachfolgend Kunde genannt -",
        'nachfolgend "Auftraggeber"',
        "Customer: ACME",
    ],
)
def test_labelled_customer_blocks_are_strong_in_basis_v4_only(line):
    assert _strong(BASIS_V4, line)


def test_bare_customer_word_stays_weak():
    assert not _strong(BASIS_V4, "Der Auftraggeber stellt die Zugänge zur Verfügung.")


# --- 4. Merge ------------------------------------------------------------------------


V4_RULES = OPT.for_version("v4")


def _merge(*results, fields=None):
    calls = [((fields[i] if fields else None), r) for i, r in enumerate(results)]
    merged, conflicts = merge_results(calls, V4_SCHEMA, V4_RULES.dependent_fields, V4_RULES.list_fields)
    jsonschema.validate(merged, V4_SCHEMA)
    return merged, conflicts


def test_merge_unions_sites_and_drops_normalized_duplicates():
    other = {"name": "Haus Sulgen", "street": "Bahnhofstr. 5", "zip": "78713", "city": "Schramberg"}
    same_spaced = {
        "name": " pflegezentrum  waldmössingen ",
        "street": "Heimbachstraße 8",
        "zip": "78713",
        "city": "SCHRAMBERG-WALDMÖSSINGEN",
    }
    merged, conflicts = _merge(_result(sites=[SITE_A]), _result(sites=[same_spaced, other]))
    assert merged["sites"] == [SITE_A, other]  # first occurrence kept, stable order
    assert "sites" not in conflicts


def test_merge_unions_assets_dedupe_by_serial_then_name_location():
    same_serial = {"name": "Personenaufzug", "serial_number": "fn123456", "location": None}
    no_serial = {"name": "RLT Küche", "serial_number": None, "location": "UG"}
    no_serial_again = {"name": "rlt  küche", "serial_number": None, "location": "ug"}
    merged, _ = _merge(_result(assets=[ASSET_A, no_serial]), _result(assets=[same_serial, no_serial_again]))
    assert merged["assets"] == [ASSET_A, no_serial]


def test_merge_drops_all_null_items():
    empty_site = {"name": None, "street": None, "zip": None, "city": " "}
    merged, _ = _merge(_result(sites=[empty_site]), _result(sites=[]))
    assert merged["sites"] == []


def test_merge_caps_lists_at_the_schema_limits():
    sites = [dict(SITE_A, name=f"Haus {i}") for i in range(8)]
    more_sites = [dict(SITE_A, name=f"Haus {i}") for i in range(5, 14)]
    assets = [{"name": f"Aufzug {i}", "serial_number": f"SN-{i}", "location": None} for i in range(40)]
    more_assets = [
        {"name": f"Aufzug {i}", "serial_number": f"SN-{i}", "location": None} for i in range(30, 70)
    ]
    merged, _ = _merge(_result(sites=sites, assets=assets), _result(sites=more_sites, assets=more_assets))
    assert [s["name"] for s in merged["sites"]] == [f"Haus {i}" for i in range(10)]
    assert [a["name"] for a in merged["assets"]] == [f"Aufzug {i}" for i in range(50)]


def test_merge_lists_come_from_the_site_chunk_when_it_ran():
    basis_fields = BASIS_V4.fields
    merged, _ = _merge(
        _result(sites=[{"name": "Briefkopf", "street": "x", "zip": "1", "city": "y"}]),
        _result(sites=[SITE_A]),
        fields=[basis_fields, SITE.fields],
    )
    assert merged["sites"] == [SITE_A]


def test_merge_lists_from_all_chunks_when_no_site_chunk_ran():
    merged, _ = _merge(_result(assets=[ASSET_A]), _result(), fields=[BASIS_V4.fields, ("amount",)])
    assert merged["assets"] == [ASSET_A]


def test_merge_conflicting_customer_name_is_null_with_a_note():
    merged, conflicts = _merge(
        _result(customer_name="Stegwiesen GmbH"), _result(customer_name="Andere GmbH"), fields=[None, None]
    )
    assert merged["customer_name"] is None and "customer_name" in conflicts
    assert "customer_name" in merged["notes"] and "Stegwiesen GmbH" in merged["notes"]
    assert not isinstance(merged["customer_name"], list)


def test_merge_same_customer_name_in_two_chunks_is_kept():
    merged, conflicts = _merge(
        _result(customer_name="Stegwiesen GmbH"), _result(customer_name="stegwiesen  gmbh")
    )
    assert merged["customer_name"] == "Stegwiesen GmbH" and conflicts == []


def test_merge_conflicting_customer_address_is_null():
    a = {"street": "Stegwiesen 13", "zip": "78333", "city": "Stockach"}
    b = {"street": "Hauptstr. 1", "zip": "78333", "city": "Stockach"}
    merged, conflicts = _merge(_result(customer_address=a), _result(customer_address=b))
    assert merged["customer_address"] is None and "customer_address" in conflicts


def test_merge_v3_path_is_unchanged_without_list_rules():
    v3 = {name: None for name in V3_SCHEMA["properties"]}
    merged, _ = merge_results(
        [(None, dict(v3, vendor_name="A")), (None, dict(v3, vendor_name="B"))],
        V3_SCHEMA,
        OPT.dependent_fields,
    )
    assert merged["vendor_name"] is None and set(merged) == set(V3_SCHEMA["properties"])


# --- 5. Evidence delivery + v3/v4 input comparison --------------------------------


PROFILE_8K = ModelProfile(
    provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=8192
)


def _run(text: str, version: str, *, context=None, answers=None, profile=PROFILE_8K):
    """Real extraction path with a mocked Ollama transport. Returns the sent
    requests, the outcome and the document text of every call."""
    requests: list[dict] = []
    schema = CONTRACT.schema_for(version)

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        if answers:
            answer = answers[min(len(requests), len(answers)) - 1]
        else:
            answer = {
                f: ([] if schema["properties"][f].get("type") == "array" else None)
                for f in schema["properties"]
            }
        return httpx.Response(200, json={"response": json.dumps(answer), "done": True})

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    outcome = run_extraction(
        mode=CONTRACT,
        prompt_template=_read(version),
        context=context or {},
        text=text,
        profile=profile,
        ollama_client=client,
        optimization_settings={MODE: TextOptimizationSettings()},
        prompt_version=version,
    )
    docs = [r["prompt"].split("DOCUMENT TEXT:\n", 1)[1].split("\n\nOUTPUT\n", 1)[0] for r in requests]
    return requests, outcome, docs


def test_structured_output_format_is_the_versions_schema():
    text = "Wartungsvertrag\nAuftraggeber: Stegwiesen GmbH\nAuftragnehmer: Lift GmbH"
    requests, _, _ = _run(text, "v3")
    assert requests[0]["format"] == V3_SCHEMA  # v3 byte-identical to before v4 existed
    requests, outcome, _ = _run(text, "v4")
    assert requests[0]["format"] == V4_SCHEMA
    assert outcome.result["sites"] == [] and outcome.result["assets"] == []


def test_without_prompt_version_the_runner_keeps_the_base_schema():
    requests: list[dict] = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, json={"response": json.dumps({f: None for f in V3_SCHEMA["properties"]}), "done": True}
        )

    client = OllamaClient("http://ollama.test")
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    run_extraction(
        mode=CONTRACT,
        prompt_template=V3,
        context={},
        text="Vertrag",
        profile=PROFILE_8K,
        ollama_client=client,
        optimization_settings={MODE: TextOptimizationSettings()},
    )
    assert requests[0]["format"] == V3_SCHEMA


SITE_CLAUSE = (
    "§ 2 Anlagenstandort\nAnlagenstandort:\nPflegezentrum Waldmössingen\nHeimbachstraße 8\n"
    "78713 Schramberg-Waldmössingen\nAufzug 2, Fabriknummer: FN-123 456, Haus A"
)
CUSTOMER_HEAD = (
    "WARTUNGSVERTRAG\n\nAuftraggeber:\nStegwiesen Pflegezentrum GmbH\nStegwiesen 13\n78333 Stockach\n\n"
    "Auftragnehmer:\nMusterlift Aufzugsservice GmbH, Beispielweg 12, 12345 Musterstadt\n"
    "Angebot Nr.: A-998877\nVertragsnummer: V-2026-0042"
)


def _long(head: str, clauses: list[str]) -> str:
    parts = [head]
    for i, clause in enumerate(clauses):
        parts += [filler_section(400 + i, 2500, dense=True), clause]
    parts += [filler_section(497 + i, 2500, dense=True) for i in range(3)]
    return "\n\n".join(parts)


def test_customer_site_and_serial_passages_reach_the_model_in_a_long_contract():
    _, outcome, docs = _run(_long(CUSTOMER_HEAD, [SITE_CLAUSE]), "v4")
    sent = "\n".join(docs)
    for line in (
        "Auftraggeber:\nStegwiesen Pflegezentrum GmbH",
        "Anlagenstandort:",
        "Heimbachstraße 8",
        "Fabriknummer: FN-123 456",
        "Angebot Nr.: A-998877",
        "Vertragsnummer: V-2026-0042",
    ):
        assert line in sent, line
    assert outcome.optimization["strategy"] in ("relevance", "chunked")


def test_context_hotel_name_is_only_context():
    context = {"document_type": "contract", "hotel_name": "Hotel Kontextname"}
    requests, _, docs = _run("Wartungsvertrag ohne Kundenangabe.", "v4", context=context)
    prompt = requests[0]["prompt"]
    context_block = prompt.split("\nCONTEXT:\n", 1)[1].split("\nDOCUMENT TEXT:\n", 1)[0]
    assert "hotel_name: Hotel Kontextname" in context_block
    assert "Hotel Kontextname" not in docs[0]
    assert "never take a name, role or address from CONTEXT" in prompt


def test_chunked_v4_run_merges_lists_and_validates():
    """A tiny context window forces chunking; each chunk answers its own
    facts, the merged result unions the lists and passes the v4 schema."""
    small = ModelProfile(
        provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=3500
    )
    text = _long(
        CUSTOMER_HEAD,
        [
            SITE_CLAUSE,
            "§ 3 Vergütung\nPreis: 450,00 € monatlich",
            "§ 4 Laufzeit\nDer Vertrag beginnt am 01.01.2026 und endet am 31.12.2027.",
            "§ 5 Standort: Haus Sulgen, Bahnhofstr. 5, 78713 Schramberg\nSeriennummer: SN 777",
        ],
    )
    empty = _result()
    answers = [
        dict(
            empty,
            vendor_name="Musterlift Aufzugsservice GmbH",
            customer_name="Stegwiesen Pflegezentrum GmbH",
            customer_address={"street": "Stegwiesen 13", "zip": "78333", "city": "Stockach"},
        ),
        dict(empty, start_date="2026-01-01", end_date="2027-12-31"),
        dict(empty, amount=450.0, currency="EUR", payment_interval="monthly"),
        dict(
            empty,
            sites=[
                SITE_A,
                {"name": "Haus Sulgen", "street": "Bahnhofstr. 5", "zip": "78713", "city": "Schramberg"},
            ],
            assets=[ASSET_A, {"name": None, "serial_number": "SN 777", "location": "Haus Sulgen"}, ASSET_A],
        ),
    ]
    requests, outcome, _ = _run(text, "v4", answers=answers, profile=small)
    assert outcome.optimization["strategy"] == "chunked", outcome.optimization
    labels = [call["label"] for call in outcome.optimization["calls"]]
    assert "site" in labels and labels[0] == "basis"
    result = outcome.result
    jsonschema.validate(result, V4_SCHEMA)
    assert result["customer_name"] == "Stegwiesen Pflegezentrum GmbH"
    assert [s["name"] for s in result["sites"]] == ["Pflegezentrum Waldmössingen", "Haus Sulgen"]
    assert [a["serial_number"] for a in result["assets"]] == ["FN-123 456", "SN 777"]
    assert all(r["format"] == V4_SCHEMA for r in requests)


# v3 vs v4 input comparison on the synthetic fixtures: every snippet the v3
# planner delivers for the 19 existing fields must still be delivered by the
# v4 planner (longer prompt, larger output reserve, extra SITE passages).
FIXTURES = {
    "short": (build_contract(3000), RELEVANT_SNIPPETS),
    "long": (build_contract(60000, dense=True), RELEVANT_SNIPPETS),
    "long_pdf": (build_pdf_contract(60000), PDF_RELEVANT_SNIPPETS),
    "service_60": (build_service_contract(60), SERVICE_CORE_SNIPPETS),
    "with_site": (
        _long(CUSTOMER_HEAD, [SITE_CLAUSE] + [c for c in RELEVANT_SNIPPETS]),
        ("Vertragsnummer: V-2026-0042",),
    ),
}


@pytest.mark.parametrize("name", sorted(FIXTURES))
@pytest.mark.parametrize("window", [8192, 16384])
def test_v4_delivers_every_existing_field_snippet_v3_delivers(name, window):
    text, snippets = FIXTURES[name]
    profile = ModelProfile(
        provider="ollama", model="m", timeout_seconds=60, temperature=0, context_window_tokens=window
    )
    _, out3, docs3 = _run(text, "v3", profile=profile)
    _, out4, docs4 = _run(text, "v4", profile=profile)
    sent3, sent4 = "\n".join(docs3), "\n".join(docs4)
    lost = [s for s in snippets if s in sent3 and s not in sent4]
    assert lost == [], (name, window, lost, out3.optimization["strategy"], out4.optimization["strategy"])


# --- 6. Routes: Lab draft schema_version, analyze with v4 active -------------------

from tests.conftest import AI_KEY, LAB_KEY, auth_headers  # noqa: E402

LAB_ANALYZE_URL = "/api/v1/lab/analyze"
ACTIVATE_URL = "/api/v1/lab/prompts/contract_extraction/activate"
ANALYZE_URL = "/api/v1/documents/analyze"


def _lab(client, **extra):
    body = {"mode": MODE, "context": {}, "text": "Wartungsvertrag", "prompt": V4, **extra}
    return client.post(LAB_ANALYZE_URL, json=body, headers=auth_headers(LAB_KEY))


def test_lab_draft_uses_the_active_versions_schema_by_default(client):
    client.fake_ollama["client"].result = {f: None for f in V3_SCHEMA["properties"]}
    response = _lab(client)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["schema_version"] == "v3"
    assert client.fake_ollama["client"].last_schema == V3_SCHEMA


def test_lab_draft_can_run_with_the_v4_schema_before_activation(client):
    client.fake_ollama["client"].result = _result(customer_name="Stegwiesen GmbH")
    response = _lab(client, schema_version="v4")
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["schema_version"] == "v4" and data["result"]["customer_name"] == "Stegwiesen GmbH"
    assert client.fake_ollama["client"].last_schema == V4_SCHEMA


@pytest.mark.parametrize("bad", ["4", "v04", "../v4", "v4.json", ""])
def test_lab_draft_rejects_a_malformed_schema_version(client, bad):
    response = _lab(client, schema_version=bad)
    assert response.status_code == 400
    assert response.json()["code"] == "invalid_prompt_version"


def test_analyze_with_v4_active_sends_the_v4_schema_and_rollback_restores_v3(client):
    fake = client.fake_ollama["client"]
    assert client.post(ACTIVATE_URL, json={"version": "v4"}, headers=auth_headers(LAB_KEY)).status_code == 200
    fake.result = _result(sites=[SITE_A])
    body = {"mode": MODE, "context": {}, "text": "Wartungsvertrag\nAnlagenstandort: Heimbachstraße 8"}
    response = client.post(ANALYZE_URL, json=body, headers=auth_headers(AI_KEY))
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["prompt_version"] == "v4" and data["result"]["sites"] == [SITE_A]
    assert fake.last_schema == V4_SCHEMA and fake.last_prompt.startswith("ROLE")
    assert "- assets:" in fake.last_prompt
    # Rollback = one activation, schema included.
    assert client.post(ACTIVATE_URL, json={"version": "v3"}, headers=auth_headers(LAB_KEY)).status_code == 200
    fake.result = {f: None for f in V3_SCHEMA["properties"]}
    response = client.post(ANALYZE_URL, json=body, headers=auth_headers(AI_KEY))
    assert response.json()["data"]["prompt_version"] == "v3" and fake.last_schema == V3_SCHEMA
    assert "- assets:" not in fake.last_prompt


# --- 7. Staging fixtures (tests/fixtures/contract_extraction_v4) -----------------
# Synthetic texts + gold results for the real-model comparison
# (`contract_diagnose run --prompts v3,v4 --gold ...`). Checked here: the gold
# only uses v4 fields, its evidence is really in the text, distractors are
# present, and the planner hands the evidence to the model.

from pathlib import Path  # noqa: E402

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "contract_extraction_v4"
FIXTURE_NAMES = sorted(p.name[: -len(".txt")] for p in FIXTURE_DIR.glob("*.txt"))


def _fixture(name):
    text = (FIXTURE_DIR / f"{name}.txt").read_text(encoding="utf-8")
    gold = json.loads((FIXTURE_DIR / f"{name}.gold.json").read_text(encoding="utf-8"))
    context = json.loads((FIXTURE_DIR / f"{name}.context.json").read_text(encoding="utf-8"))
    return text, gold, context


def test_fixture_set_is_complete():
    assert len(FIXTURE_NAMES) == 6


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_gold_is_consistent(name):
    text, gold, context = _fixture(name)
    assert set(gold) <= set(V4_SCHEMA["properties"])
    for field, expected in gold.items():
        if isinstance(expected, dict) and "contains" in expected:
            for needle in expected["contains"]:
                assert needle.lower() in text.lower(), (field, needle)
        if isinstance(expected, dict) and "excludes" in expected:
            for needle in expected["excludes"]:
                assert needle in text or needle in json.dumps(context, ensure_ascii=False), (field, needle)
    assert "hotel_id" not in json.dumps(context)


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_evidence_reaches_the_model_with_v4(name):
    text, gold, context = _fixture(name)
    _, _, docs = _run(text, "v4", context=context)
    sent = "\n".join(docs).lower()
    for field, expected in gold.items():
        for needle in expected.get("contains", []) if isinstance(expected, dict) else []:
            assert needle.lower() in sent, (name, field, needle)


def test_gold_check_handles_v4_lists_and_excludes():
    from system.tools.contract_diagnose import check_against_gold

    result = _result(
        sites=[SITE_A],
        assets=[ASSET_A],
        customer_address={"street": "Stegwiesen 13", "zip": "78333", "city": "Stockach"},
    )
    checks = check_against_gold(
        result,
        {
            "sites": {"contains": ["heimbach"]},
            "assets": {"excludes": ["A-998877"]},
            "customer_address": {"contains": ["Stockach"]},
            "customer_name": None,
        },
    )
    assert all(c["ok"] for c in checks.values()), checks
    assert not check_against_gold(result, {"assets": {"excludes": ["FN-123"]}})["assets"]["ok"]
    assert not check_against_gold(_result(), {"sites": {"contains": ["x"]}})["sites"]["ok"]
