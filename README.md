# DocPipe

Generic, authenticated document processing proxy.

```text
Client Application
      |
      v
   DocPipe  --  API key check
      |
      +---------------------------+
      |                           |
      v                           v
   Stirling                    Ollama
 (text extraction)          (AI analysis, V2)
      |                           |
      v                           v
   Response normalization (same stable shape either way)
      |
      v
Client Application
```

DocPipe's job is to be a stable boundary: client applications (HubDix,
Chronodix, or anyone else) never need to know which document processing
engine or model provider sits behind it, or how to talk to it.

## What can DocPipe do?

- API key authentication (`Authorization: Bearer <key>`)
- PDF text extraction, proxied through [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF)
- Automatic OCR fallback for scanned/image-only PDFs when plain text
  extraction comes back with too little real text - still entirely via
  Stirling (see "Text extraction" below); DocPipe orchestrates the
  fallback, it never runs OCR itself
- AI analysis of already-extracted text, proxied through [Ollama](https://ollama.com) (V2, optional per client)
- Runtime-resolved, versioned AI prompts per mode, with a `prompt_lab`-gated
  API to draft-test/save/activate a new version without an image rebuild
  or restart (V2.2, optional per client - see [docs/prompt-lab.md](docs/prompt-lab.md))
- A deterministic, keyword-routed assistant query endpoint - a technical
  foundation for a future hotel assistant, not the assistant itself
  (V2.2, optional per client - see [docs/assistant-routing.md](docs/assistant-routing.md))
- A normalized, stable success/error response contract across all of the above
- Per-client service flags loaded from a local config file (no database)

## What can DocPipe NOT do?

- No OCR engine of its own - no bundled Tesseract/OCRmyPDF, no native OCR
  dependency; OCR is fully delegated to Stirling (see "Text extraction")
- No OCR language auto-detection - `stirling.ocr.languages` is a fixed,
  configured list (`deu`+`eng` by default), not detected per document
- No AI/vision-based OCR or OCR output cleanup (no LLM touches OCR text)
- No semantic search, no RAG, no model training/fine-tuning
- No automatic document classification
- No database, no persistent jobs, no background queue
- No quotas, plans, usage tracking, or billing
- No user management, no web UI
- No persistence of uploaded documents — every request is processed
  in a temp file that is deleted immediately afterwards
- No business logic specific to any consuming project (HubDix or otherwise)
- No chunking of oversized AI input — a text over a mode's limit is
  rejected, not silently split or truncated
- No arbitrary/client-supplied prompt on the normal AI path — a draft
  prompt is only ever accepted by `/lab/analyze`, gated by its own
  `prompt_lab` permission (see [docs/prompt-lab.md](docs/prompt-lab.md))
- No database access, SQL generation, tool calling, or autonomous agent
  behavior anywhere in the assistant routing endpoint — it classifies a
  question and calls Ollama with context the caller already prepared,
  nothing else (see [docs/assistant-routing.md](docs/assistant-routing.md))

## Architecture

- **Framework**: Python + FastAPI. Small, REST-friendly, built-in OpenAPI
  docs (`/docs`), and a natural fit for an AI/Ollama path alongside the
  original Stirling path without a rewrite.
- **Version**: `0.1.0`, held in one place ([system/__init__.py](system/__init__.py)) and reused
  everywhere else (health response, FastAPI app metadata).

```text
docpipe/
├─ system/                  # application source root
│  ├─ main.py                # app wiring, middleware, error handlers
│  ├─ config.py               # YAML config + env overrides + model profiles
│  ├─ auth.py                  # API key authentication
│  ├─ errors.py                 # normalized error codes
│  ├─ schemas.py                 # request/response models
│  ├─ text_quality.py             # is_text_sufficient() - OCR-fallback trigger heuristic
│  ├─ ai/
│  │  ├─ modes.py                 # extraction mode registry (name -> schema+DEFAULT profile/prompt version)
│  │  ├─ resolver.py               # mode + client -> concrete ModelProfile (extraction AND assistant)
│  │  ├─ prompting.py               # builds the final extraction prompt from template+context+text
│  │  └─ prompt_registry.py          # runtime-resolved, versioned prompt storage (V2.2) - see docs/prompt-lab.md
│  ├─ assistant/                  # routing foundation (V2.2) - see docs/assistant-routing.md
│  │  ├─ modes.py                 # assistant mode registry (name -> DEFAULT profile/prompt version)
│  │  ├─ router.py                 # deterministic question -> mode classification
│  │  └─ prompting.py               # builds the final assistant prompt from template+context+question
│  ├─ prompts/
│  │  ├─ maintenance_extraction/
│  │  │  ├─ v1.txt                  # versioned prompt template (base/default - see docs/prompt-lab.md)
│  │  │  └─ schema.json              # response JSON Schema (not Lab-editable)
│  │  ├─ inspection_extraction/
│  │  │  ├─ v1.txt
│  │  │  └─ schema.json
│  │  └─ assistant/                # one subfolder per assistant mode, same v1.txt/active.json shape
│  │     ├─ hotel_health_summary/v1.txt
│  │     ├─ maintenance_question/v1.txt
│  │     ├─ inspection_question/v1.txt
│  │     ├─ contract_question/v1.txt
│  │     ├─ document_question/v1.txt
│  │     └─ general_hotel_question/v1.txt
│  ├─ services/
│  │  ├─ stirling.py              # the only module that knows Stirling
│  │  └─ ollama.py                 # the only module that knows Ollama
│  └─ routes/
│     ├─ health.py
│     ├─ capabilities.py
│     ├─ documents.py              # extract-text
│     ├─ analyze.py                # AI analysis (V2)
│     ├─ lab.py                    # Prompt Lab API (V2.2)
│     └─ assistant.py              # assistant query endpoint (V2.2)
├─ runtime-prompts/          # writable Prompt Lab storage (V2.2, gitignored - see docs/prompt-lab.md)
├─ tests/                    # pytest suite, Stirling and Ollama are mocked
├─ docs/
│  ├─ staging-deployment.md   # deployment guide + validation checklist
│  ├─ prompt-lab.md            # runtime prompt versioning + Lab API (V2.2)
│  └─ assistant-routing.md      # assistant routing foundation (V2.2)
├─ config.example.yaml        # template — copy to config.yaml, don't commit it
├─ .env.example
├─ Dockerfile
├─ docker-compose.yml
└─ requirements.txt
```

## Quick start

```bash
cp config.example.yaml config.yaml
# edit config.yaml: set stirling.base_url and a real client api_key
# (only if you use AI analysis) set ollama.base_url and define at least
# the model profiles your active modes default to - see "Model profiles" below

pip install -r requirements.txt
uvicorn system.main:app --host 0.0.0.0 --port 8000
```

Or with Docker Compose (spins up DocPipe + a local Stirling instance;
Ollama is included but not started by default — see below):

```bash
cp config.example.yaml config.yaml
docker compose up --build
```

Stirling and Ollama don't have to run in the same Compose stack — point
`stirling.base_url`/`ollama.base_url` (or `STIRLING_URL`/`OLLAMA_URL`) at
any existing instances instead; Compose here is convenience only.

### Try it

```bash
curl http://localhost:8000/api/v1/health

curl -H "Authorization: Bearer YOUR_KEY" \
  http://localhost:8000/api/v1/capabilities

curl -X POST \
  -H "Authorization: Bearer YOUR_KEY" \
  -F "file=@sample.pdf" \
  http://localhost:8000/api/v1/documents/extract-text

# AI analysis (only if the client has services.ai: true) - see "AI analysis" below
curl -X POST \
  -H "Authorization: Bearer YOUR_KEY" -H "Content-Type: application/json" \
  -d '{"mode":"maintenance_extraction","context":{},"text":"..."}' \
  http://localhost:8000/api/v1/documents/analyze
```

Interactive API docs are available at `/docs` once the server is running.

## Text extraction

`/documents/extract-text` tries plain (embedded) text extraction first and
only falls back to OCR when that comes back with too little real text:

```text
PDF
  |
  v
embedded text extraction (Stirling, fast path)
  |
  v
text sufficient? (is_text_sufficient() - see system/text_quality.py)
  | yes                              | no
  v                                  v
done                          OCR the PDF (Stirling)
extraction_method=                   |
  "embedded_text"                    v
                             embedded text extraction again,
                             now on the OCR'd PDF
                                      |
                                      v
                                    done
                            extraction_method="ocr"
```

- OCR is a fallback, never the default path - a normal text-based PDF
  never invokes OCR and pays no latency cost for it.
- The "text sufficient?" check (`is_text_sufficient`,
  `stirling.ocr.min_meaningful_characters`, default `30`) counts
  alphanumeric characters only, ignoring whitespace/line-break/control-
  character artifacts that a scanned PDF's plain-text extraction often
  still produces. It's a deliberately simple V1 heuristic, not a content
  classifier - a genuinely short real document can still trigger an OCR
  attempt; that's an accepted tradeoff, not a bug.
- OCR only ever runs after a *technically successful* first extraction
  that came back with too little text. A technical failure of the first
  extraction (`timeout`, `upstream_unavailable`, `upstream_auth_failed`,
  `processing_failed`) is returned as-is and never triggers OCR.
- Exactly one OCR attempt per request - no retry loop, no second OCR pass
  even if the resulting text is still short or empty. An OCR attempt that
  runs successfully but finds no recognizable text is a normal success
  response with `text: ""`, not an error; a *technical* OCR failure (or a
  failure of the extraction that follows it) is returned as a normal
  normalized upstream error, same codes as above.
- The original upload and any intermediate OCR'd PDF are temp files only,
  removed in the same `finally` block regardless of which stage fails -
  see "Files & security" below.

## Pipeline: extract-text and analyze are independent steps

`extract-text` and `analyze` are two separate, independently callable
operations — never combined into one "upload and analyze" endpoint. The
recommended consumer flow is:

```text
Document
  |
  v
extract-text  -->  consumer stores the extracted text
  |
  v
analyze       -->  consumer stores the AI result
```

If `analyze` fails (AI unavailable, timeout, invalid model output), the
already-extracted text from the first step is untouched and does not need
to be re-extracted — the two steps have fully independent failure modes.
`analyze` never re-runs text extraction and never talks to Stirling
itself; it only ever receives text the caller already has.

## Configuration

No database. Clients and their permissions live in a local YAML file:

```yaml
server:
  max_file_size_mb: 25   # technical safety limit, not a quota

stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

  ocr:                      # optional - see "Text extraction" above. Omit entirely for
                             # the pre-OCR defaults shown here (enabled, deu+eng, 30, 180).
    enabled: true
    languages: [deu, eng]
    min_meaningful_characters: 30
    timeout_seconds: 180    # separate from stirling.timeout_seconds - OCR is much slower

ollama:                    # optional - only needed if any client has services.ai: true
  base_url: "http://ollama:11434"    # provider-wide connection only, no model here

models:                    # named resource/quality classes - see "Model profiles" below
  light:
    provider: ollama
    model: "your-small-model"
    timeout_seconds: 60
    temperature: 0
  standard:
    provider: ollama
    model: "your-medium-model"
    timeout_seconds: 120
    temperature: 0
  heavy:
    provider: ollama
    model: "your-large-model"
    timeout_seconds: 180
    temperature: 0

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
      ai: true              # optional, defaults to false if omitted (V1 configs keep working unchanged)

    model_overrides:          # optional - mode name -> model PROFILE name (never a raw model name)
      maintenance_extraction: light
      inspection_extraction: standard
```

Only `config.example.yaml` is committed. The real `config.yaml` (or
whatever `DOCPIPE_CONFIG` points at) must stay out of git and should have
restrictive file permissions (`chmod 600` on the host, or a mounted
read-only secret in your orchestrator) since it holds client API keys in
plain text — deliberately, to keep administration simple. Keys are never
logged, never echoed back in error responses, and the config content is
never exposed through the API.

### Environment overrides

| Variable            | Overrides                    | Default        |
|---------------------|-------------------------------|----------------|
| `DOCPIPE_CONFIG`    | path to the YAML config file   | `config.yaml`  |
| `STIRLING_URL`      | `stirling.base_url`             | (from file)    |
| `STIRLING_API_KEY`  | `stirling.api_key`               | (from file)    |
| `STIRLING_TIMEOUT`  | `stirling.timeout_seconds`        | (from file)    |
| `OLLAMA_URL`        | `ollama.base_url`                  | (from file)    |

Priority: environment variable > value in the YAML file > built-in default.
`DOCPIPE_CONFIG` only selects *which* file is read; it does not override a
value inside it. Model profiles (`models:`) have no environment override
at all — by design, they change only through config, never through code
or an env var, so "which model backs a profile" stays a single,
reviewable place per environment (see "Model profiles" below).

Client definitions (`clients:`, including `model_overrides:`) are only
ever read from the YAML file — there is intentionally no per-client
environment override.

### Config validation (fail fast)

If AI is "configured" — meaning `models:` is non-empty, or at least one
client has `services.ai: true` or a non-empty `model_overrides` — DocPipe
validates the whole AI configuration at startup, before accepting any
request:

- every profile under `models:` has a `provider` (currently only
  `"ollama"` is supported), a non-empty `model`, `timeout_seconds > 0`,
  and a non-negative `temperature`;
- every active mode's default profile name (see `system/ai/modes.py`)
  exists under `models:`;
- every client's `model_overrides` references a real mode name and a
  real profile name.

Any violation raises `ConfigError` and the process refuses to start —
never a 500 on the first `/analyze` call that happens to hit the broken
path. A config with no AI use at all (no `models:`, no client with `ai`
or `model_overrides`) skips this validation entirely and behaves exactly
like a pre-AI DocPipe deployment.

## API

All authenticated endpoints expect:

```text
Authorization: Bearer <api-key>
```

### `GET /api/v1/health` — unauthenticated

Confirms the DocPipe process itself is up. Does not contact Stirling or Ollama.

```json
{ "ok": true, "service": "docpipe", "version": "0.1.0" }
```

### `GET /api/v1/capabilities` — authenticated

Reflects what the calling client is actually allowed to use.

```json
{
  "ok": true,
  "services": { "documents": true, "ai": true, "prompt_lab": true, "assistant": true },
  "features": ["extract_text", "ocr_fallback", "analyze", "prompt_lab", "assistant"],
  "ai_modes": ["maintenance_extraction", "inspection_extraction"],
  "assistant_modes": ["hotel_health_summary", "maintenance_question", "inspection_question", "contract_question", "document_question", "general_hotel_question"]
}
```

`ai_modes`/`assistant_modes` are only present (non-null) when
`services.ai`/`services.assistant` is true for the calling client.
`ocr_fallback` is only present when `documents` is enabled for the client
*and* `stirling.ocr.enabled` is true server-wide (OCR is a server
capability, not a per-client flag). `prompt_lab` and `assistant` are both
independent opt-ins from `ai` - a client needs the matching flag even if
it already has `ai: true`.

### `POST /api/v1/documents/extract-text` — authenticated

Multipart upload, field name `file`. Accepts `application/pdf` only
(verified by content, not by file extension or the declared
`Content-Type`). Transparently falls back to OCR for scanned/image-only
PDFs - see "Text extraction" above. Success:

```json
{
  "ok": true,
  "data": {
    "text": "...",
    "text_length": 1234,
    "extraction_method": "embedded_text"
  }
}
```

`extraction_method` is additive metadata, always one of `"embedded_text"`
(first-pass extraction already had enough text) or `"ocr"` (the OCR
fallback ran). Existing consumers that don't read this field are
unaffected - nothing about the rest of the contract changed.

### `POST /api/v1/documents/analyze` — authenticated, V2

Only for clients with `services.ai: true`. See "AI analysis" below for
the full request/response contract, modes, and error codes. Never
accepts a client-supplied prompt - see "Prompt Lab (V2.2)" below for the
one endpoint that does.

### Prompt Lab endpoints — authenticated, V2.2

Only for clients with `services.prompt_lab: true`
(`/lab/analyze` additionally needs `services.ai: true`). Full contract in
[docs/prompt-lab.md](docs/prompt-lab.md):

```text
GET  /api/v1/lab/prompts                          - every mode's active version + all known versions
GET  /api/v1/lab/prompts/{mode}                    - load a version's content (?version=, defaults to active)
POST /api/v1/lab/analyze                           - test a DRAFT prompt (never persisted, never the active version)
POST /api/v1/lab/prompts/{mode}/versions           - save a new, immutable version
POST /api/v1/lab/prompts/{mode}/activate           - make an existing version active (never automatic on save)
```

### `POST /api/v1/assistant/query` — authenticated, V2.2

Only for clients with `services.assistant: true`. Routes a free-text
question to one of a fixed set of assistant modes and answers it strictly
from caller-supplied `context` - full contract, trust boundary, and
grounding rules in [docs/assistant-routing.md](docs/assistant-routing.md).
Not a full hotel assistant - a routing/prompting foundation for one.

### Error contract

Every error, from any endpoint, has the same shape:

```json
{ "ok": false, "code": "upstream_unavailable", "message": "Document processing service is temporarily unavailable." }
```

| code                    | HTTP status | Meaning                                        |
|-------------------------|-------------|--------------------------------------------------|
| `unauthorized`          | 401         | API key missing, unknown, or client disabled      |
| `service_disabled`      | 403         | Client is known but `documents` isn't enabled     |
| `invalid_file`          | 400         | Missing file, empty file, or not a real PDF       |
| `invalid_request`       | 400         | Malformed JSON body (e.g. on `/documents/analyze`) |
| `file_too_large`        | 413         | Upload exceeds `server.max_file_size_mb`          |
| `upstream_unavailable`  | 502         | Stirling unreachable or returned a 5xx            |
| `upstream_auth_failed`  | 502         | Stirling rejected DocPipe's own credentials       |
| `processing_failed`     | 502         | Stirling reachable but returned another error     |
| `timeout`               | 504         | Stirling did not respond within the timeout       |
| `ai_disabled`           | 403         | Client is known but `ai` isn't enabled            |
| `unknown_mode`          | 400         | `mode` is not a registered AI mode                |
| `input_too_large`       | 413         | `text` exceeds the mode's `max_input_length`      |
| `ai_unavailable`        | 502         | Ollama unreachable or returned a 5xx              |
| `ai_timeout`            | 504         | Ollama did not respond within the timeout         |
| `ai_invalid_response`   | 502         | Model output was not valid JSON matching the schema, even after one repair attempt |
| `ai_processing_failed`  | 500         | Ollama reachable but returned another error       |
| `model_profile_unavailable` | 502     | Resolved model profile name has no `models:` entry (config/runtime drift - see below) |
| `prompt_lab_disabled`   | 403         | Client is known but `prompt_lab` isn't enabled    |
| `unknown_prompt_version` | 404        | Requested/activated prompt version doesn't resolve (runtime or base) |
| `invalid_prompt_version` | 400        | Malformed version name on save (see docs/prompt-lab.md "Version naming") |
| `prompt_version_exists` | 409         | Save target already exists - versions are immutable |
| `invalid_prompt`        | 400         | Empty prompt content, or a template that fails to render |
| `prompt_too_large`      | 413         | Prompt content/draft exceeds `prompts.max_content_length` |
| `assistant_disabled`    | 403         | Client is known but `assistant` isn't enabled     |
| `internal_error`        | 500         | Unexpected DocPipe-side failure                   |

`model_profile_unavailable` should, in practice, never happen: the same
condition it checks for is already caught by startup validation (see
"Config validation" above). It exists as a defense-in-depth check inside
the resolver itself, not as a normal client-facing error path.

No stack traces, no upstream secrets, and no raw Stirling/Ollama payloads
ever reach the client — that boundary is one of DocPipe's main jobs. This
is a single, stable format used by every endpoint — there is no
alternative `{"error": {"code": ...}}` shape anywhere in DocPipe; `code`
and `message` are always top-level.

## Stirling integration

- Text extraction endpoint: `POST {stirling.base_url}/api/v1/convert/pdf/text`,
  multipart field `fileInput`, form field `outputFormat=txt`, verified
  directly against the current Stirling-PDF source (`ConvertPDFToOffice`
  controller under `/api/v1/convert`).
- OCR endpoint: `POST {stirling.base_url}/api/v1/misc/ocr-pdf`, multipart
  field `fileInput`, form fields `languages` (repeated, one per configured
  language code - e.g. `languages=deu` + `languages=eng`, **not** a single
  combined `"deu+eng"` value), `ocrType=skip-text` (only OCRs pages that
  don't already have extractable text), `ocrRenderType=hocr`. Response is
  a single PDF (`sidecar` left at its default `false`, so Stirling never
  returns a zip) - verified directly against the Stirling-PDF **2.14.2**
  source tag (`OCRController` + `ProcessPdfWithOcrRequest` under
  `/api/v1/misc`), not assumed from an older version or from the UI docs
  alone. Runs synchronously, same as text extraction (no `async=true` is
  sent, so Stirling returns the result directly instead of a job ID).
- Auth to Stirling: `X-API-KEY` header, DocPipe's own server-side key —
  the calling client never sees or supplies it. Used for both endpoints.
- Timeout: `stirling.timeout_seconds` (default 90s) for text extraction;
  OCR uses its own, separate `stirling.ocr.timeout_seconds` (default
  180s) since rendering every page to an image and running Tesseract/
  OCRmyPDF is substantially slower - see "Text extraction" above. Neither
  is overridable per request by the calling client.
- Redirects from Stirling are not followed (the upstream URL is
  server-configured, never client-controlled).
- All of this lives in exactly one place: [system/services/stirling.py](system/services/stirling.py).
  Nothing else in the codebase talks HTTP to Stirling or sees its raw
  response shape - the OCR decision logic (system/text_quality.py) and the
  route handler (system/routes/documents.py) only ever call
  `StirlingClient.extract_text()` / `.ocr_pdf()`.

DOC/DOCX extraction is intentionally not exposed: only PDF text
extraction (and, as of this pass, PDF OCR) has been verified against the
current Stirling API.

## AI analysis (V2)

Optional, per-client, and independent from document processing:
`services.ai: true` is required in addition to (or instead of)
`services.documents: true`. Ollama must be configured and reachable for
this endpoint to work; DocPipe never falls back to any other provider.

### Modes

DocPipe does not accept arbitrary prompts. A client can only select one
of a fixed, server-defined set of **modes** (see
[system/ai/modes.py](system/ai/modes.py)); each mode owns its own
versioned prompt template and response JSON Schema under
[system/prompts/](system/prompts/):

| mode                     | default prompt version | fields extracted |
|--------------------------|-------------------------|-------------------|
| `maintenance_extraction` | `v1`                    | `vendor_name`, `service_type`, `performed_at`, `next_due_date`, `technician`, `result`, `cost`, `currency`, `notes` |
| `inspection_extraction`  | `v1`                    | `inspection_type`, `inspection_date`, `next_due_date`, `vendor_name`, `inspector`, `result`, `defects_found`, `certificate_number`, `notes` |

"Default" because the prompt *text* is no longer fixed at deploy time
(V2.2): each mode's actually-active version is runtime-resolved on every
request and can be changed (draft-tested, saved, activated) through the
`prompt_lab`-gated Lab API without a rebuild or restart - see
[docs/prompt-lab.md](docs/prompt-lab.md). The response schema and
`max_input_length` shown per mode are still fixed here in code, not
Lab-editable.

More modes (`contract_extraction`, `project_offer_extraction`,
`document_summary`, ...) are anticipated by this same registry structure
but are **not implemented** — registering one is adding a prompt +
schema + registry entry, nothing else in the request pipeline changes.

### Request

```json
{
  "mode": "maintenance_extraction",
  "context": {
    "hotel_name": "Hotel Berlin Mitte",
    "asset_name": "Aufzug 1",
    "taxonomy": "Aufzugswartung",
    "known_vendor": "KONE GmbH"
  },
  "text": "..."
}
```

- `mode` and `text` are required; `context` is optional and none of its
  fields are required — it is an open bag of hints, not a fixed schema.
- **The client selects the task. DocPipe selects the model.** The
  request schema (`AnalyzeRequest`) accepts `mode`, `context`, and `text`
  and *rejects* (`invalid_request`, HTTP 400) anything else - a request
  containing `model`, `model_profile`, `provider`, `temperature`,
  `timeout`, `system_prompt`, or `prompt` fails validation outright. It
  isn't a case of those fields being silently ignored; they simply aren't
  legal request shape, by construction (pydantic `extra="forbid"`).

**Context is a hint, never a fact.** Every prompt explicitly instructs
the model: use context only to resolve ambiguity in the document text,
and never report a context value as an extracted fact unless the
document text itself supports it. If `context.taxonomy` says
"Brandschutz" but the document doesn't mention it, the model must not
invent a matching field value from context alone.

**Never guess.** Every prompt instructs the model to output `null`
rather than infer a missing value — no invented next-due-dates, no
estimated costs, no assumed vendors, no assumed results. Dates are only
normalized to `YYYY-MM-DD` when the document text clearly states one;
otherwise `null`.

**No IDs.** The model never outputs `hotel_id`/`vendor_id`/`taxonomy_id`
or any other internal identifier — only plain factual text/numbers/
booleans. Resolving a factual value (e.g. a vendor name) to an internal
ID is the consuming system's job, not DocPipe's.

### Response

```json
{
  "ok": true,
  "data": {
    "mode": "maintenance_extraction",
    "model_profile": "light",
    "model": "qwen2.5:1.5b-instruct",
    "prompt_version": "v1",
    "result": {
      "vendor_name": "KONE GmbH",
      "service_type": "Aufzugswartung",
      "performed_at": "2026-09-12",
      "next_due_date": null,
      "technician": null,
      "result": "ohne Beanstandung",
      "cost": null,
      "currency": null,
      "notes": null
    }
  }
}
```

`model_profile`, `model`, and `prompt_version` are metadata only, for
traceability and later benchmarking - never the prompt text itself, the
repair prompt, or Ollama's raw response. Nothing about `stirling.base_url`,
`ollama.base_url`, or any API key is ever reachable through this or any
other response.

`prompt_version` is resolved fresh on every request (V2.2) - it's
whichever version is currently *active* for that mode, not a value baked
in at deploy time. See [docs/prompt-lab.md](docs/prompt-lab.md) for how a
version becomes active and why this needs no cache/restart to take
effect.

## Model profiles

The core principle, one level below "the client selects the task": **the
client selects a mode; DocPipe - never the client - selects the model**,
by resolving that mode to a named model profile.

```text
Mode (client-visible, e.g. "maintenance_extraction")
  |
  v
Model Profile (server-config, e.g. "light")           <- system/ai/resolver.py
  |
  v
Concrete model (config-only, e.g. "qwen2.5:1.5b-instruct")
```

A profile is a resource/quality class, not a vendor or a specific model:

```yaml
models:
  light:
    provider: ollama
    model: "qwen3:4b"        # whatever you actually have pulled
    timeout_seconds: 60
    temperature: 0
  standard:
    provider: ollama
    model: "qwen3:8b"
    timeout_seconds: 120
    temperature: 0
  heavy:
    provider: ollama
    model: "mistral:latest"
    timeout_seconds: 180
    temperature: 0
```

`light` is not "Qwen"; `heavy` is not "Mistral" - the example above just
shows one possible assignment. Swapping which model backs `light` never
touches `system/ai/modes.py`, any prompt, or any route handler - only the
YAML file.

`provider` is a real, validated field (only `"ollama"` is implemented
right now) so a second provider can be added later without reshaping the
config format or the `ModelProfile` dataclass - no second provider
implementation exists in this pass, this is only about not painting the
config into a corner.

### Mode -> profile routing

Each mode has one hardcoded default **profile name** (not a model name)
in `system/ai/modes.py`:

| mode                     | default profile | why |
|--------------------------|------------------|-----|
| `maintenance_extraction` | `light`          | Mostly straightforward structured extraction from a short-to-medium service report. |
| `inspection_extraction`  | `standard`       | Wording and structure vary more; often needs more semantic judgement (e.g. telling a real defect finding apart from boilerplate). |

`contract_extraction`, `project_offer_extraction`, and similar
more-complex future modes are expected to default to `heavy` - which is
exactly why `heavy` is defined as a profile class already, even though no
active mode uses it yet (see "Not implemented" below: `contract_extraction`
itself is not built in this pass).

### Client overrides

A client can bump (or lower) a specific mode's profile for *itself only*,
by profile name - never by raw model name:

```yaml
clients:
  hubdix:
    ...
    model_overrides:
      maintenance_extraction: light      # explicit, same as the default
      inspection_extraction: standard
```

```yaml
model_overrides:
  maintenance_extraction: "qwen3:14b"    # INVALID - rejected at startup, not a profile name
```

**Resolution order** (`system/ai/resolver.py`, the only place this logic
exists):

1. the calling client's `model_overrides` entry for this mode, if present;
2. otherwise, the mode's own default profile;
3. otherwise, `DocPipeError("model_profile_unavailable")` — **never** a
   silent fallback to some other profile or a hardcoded model. If a
   client's override points at a profile that isn't defined, DocPipe does
   not fall back to that mode's own default either - an invalid override
   is a hard error, not a soft downgrade.

### Multi-project design

One DocPipe instance can serve multiple, unrelated client projects, each
with its own routing, without any of them knowing a concrete model name:

```text
HubDix         -> maintenance_extraction -> light (its own default)
Project B      -> maintenance_extraction -> standard (its own override)
```

Both clients send the exact same request shape (`mode`, `context`,
`text`); only DocPipe's config decides which actually runs where.

### Structured output & validation

- The request to Ollama passes the mode's JSON Schema as the `format`
  field (Ollama's own grammar-constrained structured-output feature,
  stable since v0.5.0) — the strongest available guarantee before
  DocPipe's own validation runs as a second, independent check.
- DocPipe always parses the model's output and validates it against the
  same schema server-side; the raw model response is never forwarded
  to the client as-is.
- If parsing or schema validation fails, DocPipe makes **exactly one**
  repair attempt (a follow-up prompt telling the model its previous
  output was invalid and to return the exact schema) — never an
  unbounded retry loop. If the repair attempt also fails, the request
  fails with `ai_invalid_response`.

### Ollama integration

- Endpoint: `POST {ollama.base_url}/api/generate`, `{"model", "prompt",
  "stream": false, "format": <schema>, "options": {"temperature": ...}}` —
  verified against Ollama's current API docs.
- `OllamaClient` (`system/services/ollama.py`) only knows the
  provider-wide `base_url`. `model`, `timeout_seconds`, and `temperature`
  are resolved per request (mode + calling client -> model profile, see
  "Model profiles" above) and passed in on each call - the HTTP client
  itself has no notion of modes, profiles, or client overrides, and
  cannot be reached without those parameters.
- Temperature defaults to `0` per profile for deterministic extraction;
  change it per profile in `models:` if you ever need to.
- All Ollama-specific knowledge lives in exactly this one file. Nothing
  else in the codebase talks HTTP to Ollama or sees its raw response
  shape.
- Model choice is entirely a config concern: pick any locally available
  instruct model(s) that support Ollama's structured outputs — nothing in
  DocPipe hardcodes a specific model family, and different profiles are
  free to point at the very same underlying model file if you don't need
  three distinct model files yet.

## API boundary (what each side is allowed to know)

```text
Client knows:            DocPipe knows:
  mode                      the resolved model profile
  context                   provider
  text                      model
                            temperature
                            timeout
                            prompt template (versioned, per mode)
                            response JSON Schema (per mode)
```

A client can name a *task*; it can never see or influence *how* that task
is carried out. This is what makes "one DocPipe, many client projects"
possible without any client needing to track model names, prompt
wording, or provider details - see "Multi-project design" above.

## Files & security

- Uploads are validated by content, not extension: the first bytes must
  be a real PDF signature (`%PDF-`).
- Each upload is written to a temp file with a random name (`tempfile.mkstemp`) —
  the client-supplied filename is never used to build a path, so a
  crafted filename can't cause path traversal.
- The temp file is deleted after the request, on both the success and
  the error path. DocPipe never persists an uploaded document. When the
  OCR fallback runs, the OCR'd PDF Stirling returns is held only in
  memory for the immediate follow-up extraction call - it is never
  written to disk and never persisted.
- Uploads are streamed to that temp file in chunks with a running size
  check, so an oversized upload is rejected without needing to be fully
  buffered in memory.
- AI input has its own size cap per mode (`max_input_length`, no
  chunking) — a text over that limit is rejected outright.
- No retries: if Stirling or Ollama is unavailable, DocPipe answers
  immediately with the matching error code. Retrying is left to the
  calling client.
- DocPipe is stateless — no DB, no queue, no persisted jobs, no usage
  tracking. Request in, response out.

## Logging

Structured, one line per request: timestamp, request ID, client ID,
endpoint, HTTP status, duration. A `X-Request-ID` response header is set
on every response for correlating client-side and server-side logs.
`/documents/extract-text` additionally logs one line per successful
request with input size, initial (pre-OCR) and final text *lengths*
(never the text itself), whether OCR was attempted, the resulting
`extraction_method`, and duration.

Never logged: API keys, the `Authorization` header, the Stirling/Ollama
API keys, document contents/text (extracted or OCR'd), the uploaded
filename, AI prompts, or AI output.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check system tests
pytest
```

The suite mocks both Stirling and Ollama entirely (`tests/conftest.py`,
`tests/test_ollama_client.py`, `tests/test_stirling_client.py`) and
covers: auth (missing/invalid/disabled key, disabled service for both
`documents` and `ai`), file validation (wrong MIME, oversized upload),
the Stirling failure modes (unavailable, timeout, auth failure), the OCR
fallback (`tests/test_api.py` - scanned/whitespace-only/short-real-text
text triggering OCR, a normal text-based PDF never invoking it, a
technical failure of the first extraction never triggering it, a failure
of the OCR call itself or of the post-OCR extraction each surfacing as a
normal normalized error, a technically successful OCR with no
recognizable text still returning a normal empty-text success,
`ocr.enabled: false` skipping the fallback entirely, and that OCR's own
temp-file cleanup holds under every one of those failure branches), the
OCR heuristic itself (`tests/test_text_quality.py`), the real Stirling
HTTP request shape for both endpoints including the repeated `languages`
multipart fields and the separate OCR timeout (`tests/test_stirling_client.py`),
the AI failure modes (unavailable, timeout, invalid/malformed model
output with and without a successful repair, processing failure), mode
registry validation (unknown mode, input too large, invalid context
type), model profile config validation (`tests/test_config.py` - missing
model, invalid timeout, unsupported provider, unknown mode/profile
references, fail-fast on inconsistent config, OCR config
defaults/overrides and backward compatibility with a pre-OCR config),
model resolution (`tests/test_resolver.py` - mode defaults, client
overrides, no silent fallback on an invalid profile), strict request
rejection of client-supplied model/temperature/prompt fields, both
extraction modes' schemas, a mocked success path for each, that no
secrets/prompts/document text leak into error responses, and that no
temp files are left behind after a request. No test requires a running
Stirling or Ollama instance.

Prompt Lab (`tests/test_prompt_registry.py` - the registry directly:
version name validation incl. path-traversal strings, runtime-over-base
layering, save/duplicate-rejected/invalid-name-rejected/too-large-
rejected, activate/activate-missing-rejected, hot reload without a
restart; `tests/test_lab.py` - the same at the HTTP layer plus
`prompt_lab`/`ai` permission gating on every route, save/activate
path-traversal rejected as `unknown_prompt_version`, a malformed draft/
saved template rejected, a draft never persisting or touching the active
version, and `/documents/analyze` continuing to reject a `prompt` field)
and the assistant routing foundation (`tests/test_assistant_router.py` -
all German/English examples from the task's routing acceptance list plus
the document-vs-maintenance-keyword priority case;
`tests/test_assistant.py` - `assistant` permission, correct mode/model-
profile/prompt-version resolution, the request `context` object never
mutated, question/context size limits, the three Ollama failure-mode
mappings, and that neither the question nor `context` leak into a
response or error) round out the suite.

If you do have real Stirling/Ollama instances available locally, a
manual end-to-end check with an actual PDF and a few real documents per
AI mode is worthwhile before deploying — see
[docs/staging-deployment.md](docs/staging-deployment.md) for a checklist.

## Not implemented (by design)

Semantic search, RAG, model training/fine-tuning, automatic document
classification, a database, quotas/plans/usage/billing, user management,
a web UI, persistent job storage, input chunking for oversized AI text,
a second model provider (the `provider` field exists for one, but only
`"ollama"` is implemented), `contract_extraction`/other `heavy`-profile
modes (the `heavy` profile class exists so they can be added later
without an architecture change, but none is built yet), request
concurrency control/queueing beyond what Ollama itself does, and any
business logic belonging to a specific consumer project (hotels,
maintenance, contracts, categories, etc.). HubDix or Chronodix may use
DocPipe as a client, but DocPipe has no knowledge of either.

Specific to the OCR fallback: OCR language auto-detection (the language
list is fixed server config, `deu`+`eng` by default), AI/vision-based OCR
or OCR text cleanup, table/layout-aware extraction, handwriting
recognition, multi-page parallel OCR, an OCR retry loop (exactly one
attempt per request), and any OCR request queueing beyond what this
8 GB target server and Stirling itself already impose.

Specific to the Prompt Lab (see [docs/prompt-lab.md](docs/prompt-lab.md)
"Not implemented"): response-schema editing, per-client prompt version
pinning, and version deletion/rollback beyond re-activating an older
version.

Specific to the assistant routing foundation (see
[docs/assistant-routing.md](docs/assistant-routing.md) "Not
implemented"): the actual production HubDix integration, any database/
SQL/tool access, RAG/document retrieval, an LLM-based router, per-client
assistant model-profile overrides, and Prompt Lab editing for assistant
prompts specifically (they're runtime-versioned like extraction prompts,
just not yet reachable through `/lab/...`).
