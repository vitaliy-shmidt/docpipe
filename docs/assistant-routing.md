# Assistant Routing Foundation

A controlled technical foundation for a future HubDix hotel assistant -
**not** the assistant itself. This pass builds exactly one endpoint,
`POST /api/v1/assistant/query`, that turns a free-text question into a
routed, grounded, single-answer AI call using the same model-routing and
Ollama machinery every extraction mode already uses. It deliberately does
not query any database, does not generate SQL, and does not fetch or
search documents on its own - see "Trust boundary" below.

## Pipeline

```text
question
  -> system/assistant/router.py::route()          deterministic, keyword-based
  -> system/assistant/modes.py::AssistantMode      fixed mode -> model_profile default
  -> system/ai/resolver.py::resolve_model_profile()   REUSED, unchanged
  -> system/ai/prompt_registry.py                  active version, runtime-resolved (REUSED, unchanged)
  -> system/assistant/prompting.py::build_assistant_prompt()
  -> OllamaClient.generate_structured()            REUSED, unchanged
  -> answer
```

Every "REUSED, unchanged" step is the literal same code extraction modes
call - there is no second, parallel model-resolution or Ollama-calling
implementation for the assistant.

## Modes

| mode | default profile | intent |
|---|---|---|
| `maintenance_question` | light | a question about maintenance items |
| `inspection_question` | light | a question about inspections/certifications |
| `contract_question` | light | a question about vendor/service contracts |
| `document_question` | standard | a question about a specific document's content |
| `hotel_health_summary` | standard | an overall technical-state summary |
| `general_hotel_question` | light | fallback - doesn't clearly match a more specific mode |

Unlike extraction modes, there is **no per-client model-profile
override** for assistant modes yet (see "Not implemented") - every
`assistant`-enabled client gets the same defaults. `model_profile` is a
hardcoded default in `system/assistant/modes.py`, exactly like extraction
modes in `system/ai/modes.py` - `models:` in `config.yaml` still defines
what each profile name actually resolves to; no separate
`assistant_modes:` config section exists, by design (one less place for
mode->profile routing logic to live).

## Routing - deterministic, not an LLM call

`system/assistant/router.py` classifies the question with a small,
ordered keyword list - no model call, zero latency/cost of its own, and a
wrong match is a keyword list to fix, not an opaque model decision. Rule
order matters: a document-intent keyword ("Bericht"/"report") is checked
**before** any domain-specific keyword, so e.g. *"Was steht im letzten
Wartungsbericht?"* ("Wartungsbericht" contains both a maintenance word
and a document word) resolves to `document_question`, not
`maintenance_question` - the question is about a specific document's
content, not maintenance data in general
(`tests/test_assistant_router.py::test_document_intent_wins_over_maintenance_keyword_in_same_question`).

The result (`AssistantRoute`) carries `mode` and `matched_rule` (which
keyword rule fired, or `"fallback"`) - **no confidence score**. There is
no real probabilistic signal behind a keyword match, so none is invented.

## Trust boundary - "context is server-controlled data, not client-supplied hints"

This is the one place `context` means something different from
`/documents/analyze`'s context:

- **Extraction** `context` is a small bag of short disambiguation hints
  the *client* supplies (a hotel name, a suspected vendor) - never
  trusted as fact, only used to resolve ambiguity in document text the
  model can already see.
- **Assistant** `context` is expected to be the actual supporting data
  (already-fetched hotel/maintenance/inspection/defect/contract/document
  facts) that the answer must be grounded in - prepared and supplied by
  the *calling system* (HubDix), not the end user. DocPipe trusts it as
  the sole source of truth for the answer and instructs the model
  accordingly (every assistant prompt: "Use ONLY the supplied CONTEXT...
  Never invent... If CONTEXT does not contain the answer, say so").

DocPipe itself never queries a database, never calls HubDix, never
searches or fetches a document, and never runs a tool - it only ever
sees whatever `context` the caller already decided to hand it.
Permissions, hotel scope, and actually fetching the data are entirely the
calling system's responsibility, every single call - see task
requirement "HubDix ist später für Berechtigungen/Hotel-Scope/
Datenabfrage/Datenaufbereitung zuständig."

`context` is JSON-serialized (`json.dumps(..., indent=2)`,
`system/assistant/prompting.py::render_context_block`) rather than
flattened into `key: value` lines the way extraction context is - nested
structures (a whole maintenance-items list) render far more reliably that
way than a flattened line ever would.

## Grounding rules (every assistant prompt, `system/prompts/assistant/*/v1.txt`)

- Use only the supplied context.
- Never invent, assume, or guess a fact not explicitly present in it.
- If the context doesn't contain the answer, say so plainly.
- Distinguish stated facts from any recommendation/assessment.
- Never claim something is compliant, resolved, or up to date unless the
  context explicitly supports that.
- Never claim an overdue/open/failed item has been resolved unless the
  context explicitly says so.
- Flag incomplete context instead of silently answering as if it were
  complete.
- No claimed database/tool/file access beyond what's already in context.

## Output - plain-text answer, not a business schema

V1 output is a single natural-language answer, not a structured business
decision. Rather than adding a second, schema-less code path to
`OllamaClient` for this, every assistant mode shares one minimal internal
schema (`system/assistant/modes.py::ANSWER_SCHEMA`,
`{"answer": "<string>"}`) and reuses `generate_structured()` completely
unchanged - the exact same grammar-constrained decoding, schema
validation, and single repair attempt every extraction mode already gets,
just for a one-field answer object instead of a business-fact object. The
route unwraps `result["answer"]` into the response's plain `answer`
field.

## Request / response

```json
POST /api/v1/assistant/query
{ "question": "Wie steht mein Hotel technisch da?", "context": { "maintenance_overdue": 2, "open_defects": 3 } }
```

```json
{
  "ok": true,
  "data": {
    "assistant_mode": "hotel_health_summary",
    "matched_rule": "hotel_health_keywords",
    "model_profile": "standard",
    "model": "qwen2.5:1.5b-instruct",
    "prompt_version": "v1",
    "answer": "..."
  }
}
```

`question` max 5,000 characters, `context` max 100,000 characters once
JSON-serialized (both `input_too_large`, 413) - bounded, not tuned; a
real question is a short sentence, a real context payload is a prepared
summary, never a database dump.

## Permissions

New, independent client flag `services.assistant` (`assistant_disabled`,
403, if missing) - like `prompt_lab`, a separate opt-in from `ai`, not
implied by it. `capabilities` reports `assistant_modes` (the fixed mode
name list) only for an assistant-enabled client, same discovery principle
as `ai_modes`.

## Not implemented (by design - this is a foundation, not the assistant)

- The actual HubDix-facing "Frag HubDix" feature/button - this pass ships
  the endpoint DocPipe exposes, not a HubDix integration that calls it in
  production.
- Any database access, SQL generation, tool calling, function calling,
  recursive planning, autonomous loops, or web access - the router
  classifies text, the endpoint calls Ollama with server-trusted context;
  nothing else.
- RAG / document retrieval - `document_question` answers from whatever
  document content the caller already put in `context`, it does not go
  find or search a document itself.
- An LLM-based router (V1 is deliberately rule-based - see "Routing"
  above).
- Per-client assistant model-profile overrides (see "Modes" above).
- Prompt Lab editing for assistant prompts - they're versioned and
  runtime-resolved via the same `system/ai/prompt_registry.py` extraction
  prompts use, but no `/lab/...`-style API exists to list/save/activate
  an assistant prompt version yet. Changing one today means editing
  `system/prompts/assistant/<mode>/v1.txt` (or adding a new base version)
  like any other repo file.
