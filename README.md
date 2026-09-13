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
- AI analysis of already-extracted text, proxied through [Ollama](https://ollama.com) (V2, optional per client)
- A normalized, stable success/error response contract across both
- Per-client service flags loaded from a local config file (no database)

## What can DocPipe NOT do?

- No OCR orchestration beyond what Stirling itself provides
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
│  ├─ config.py               # YAML config + env overrides
│  ├─ auth.py                  # API key authentication
│  ├─ errors.py                 # normalized error codes
│  ├─ schemas.py                 # request/response models
│  ├─ ai/
│  │  ├─ modes.py                 # mode registry (name -> prompt+schema+limits)
│  │  └─ prompting.py              # builds the final prompt from mode+context+text
│  ├─ prompts/
│  │  ├─ maintenance_extraction/
│  │  │  ├─ v1.txt                  # versioned prompt template
│  │  │  └─ schema.json              # response JSON Schema
│  │  └─ inspection_extraction/
│  │     ├─ v1.txt
│  │     └─ schema.json
│  ├─ services/
│  │  ├─ stirling.py              # the only module that knows Stirling
│  │  └─ ollama.py                 # the only module that knows Ollama
│  └─ routes/
│     ├─ health.py
│     ├─ capabilities.py
│     ├─ documents.py              # extract-text
│     └─ analyze.py                # AI analysis (V2)
├─ tests/                    # pytest suite, Stirling and Ollama are mocked
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
# (only if you use AI analysis) set ollama.base_url and ollama.model too

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

ollama:                    # optional - only needed if any client has services.ai: true
  base_url: "http://ollama:11434"
  model: "your-model"
  timeout_seconds: 120
  temperature: 0

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
      ai: true              # optional, defaults to false if omitted (V1 configs keep working unchanged)
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
| `OLLAMA_MODEL`      | `ollama.model`                      | (from file)    |
| `OLLAMA_TIMEOUT`    | `ollama.timeout_seconds`             | (from file)    |

Priority: environment variable > value in the YAML file > built-in default.
`DOCPIPE_CONFIG` only selects *which* file is read; it does not override a
value inside it. `ollama.temperature` has no environment override (kept
minimal — one more knob that in practice is always left at 0 for
extraction tasks; change it in the YAML file if you ever need to).

Client definitions (`clients:`) are only ever read from the YAML file —
there is intentionally no per-client environment override.

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
  "services": { "documents": true, "ai": true },
  "features": ["extract_text", "analyze"],
  "ai_modes": ["maintenance_extraction", "inspection_extraction"]
}
```

`ai_modes` is only present (non-null) when `services.ai` is true for the
calling client.

### `POST /api/v1/documents/extract-text` — authenticated

Multipart upload, field name `file`. Accepts `application/pdf` only
(verified by content, not by file extension or the declared
`Content-Type`). Success:

```json
{
  "ok": true,
  "data": { "text": "...", "text_length": 1234 }
}
```

### `POST /api/v1/documents/analyze` — authenticated, V2

Only for clients with `services.ai: true`. See "AI analysis" below for
the full request/response contract, modes, and error codes.

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
| `internal_error`        | 500         | Unexpected DocPipe-side failure                   |

No stack traces, no upstream secrets, and no raw Stirling/Ollama payloads
ever reach the client — that boundary is one of DocPipe's main jobs. This
is a single, stable format used by every endpoint — there is no
alternative `{"error": {"code": ...}}` shape anywhere in DocPipe; `code`
and `message` are always top-level.

## Stirling integration

- Endpoint: `POST {stirling.base_url}/api/v1/convert/pdf/text`, multipart
  field `fileInput`, form field `outputFormat=txt`, verified directly
  against the current Stirling-PDF source (`ConvertPDFToOffice` controller
  under `/api/v1/convert`).
- Auth to Stirling: `X-API-KEY` header, DocPipe's own server-side key —
  the calling client never sees or supplies it.
- Timeout: `stirling.timeout_seconds` (default 90s), enforced by DocPipe;
  clients cannot override it per request.
- Redirects from Stirling are not followed (the upstream URL is
  server-configured, never client-controlled).
- All of this lives in exactly one place: [system/services/stirling.py](system/services/stirling.py).
  Nothing else in the codebase talks HTTP to Stirling or sees its raw
  response shape.

DOC/DOCX extraction is intentionally not exposed: only PDF text
extraction has been verified against the current Stirling API.

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

| mode                     | prompt version | fields extracted |
|--------------------------|----------------|-------------------|
| `maintenance_extraction` | `v1`           | `vendor_name`, `service_type`, `performed_at`, `next_due_date`, `technician`, `result`, `cost`, `currency`, `notes` |
| `inspection_extraction`  | `v1`           | `inspection_type`, `inspection_date`, `next_due_date`, `vendor_name`, `inspector`, `result`, `defects_found`, `certificate_number`, `notes` |

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
- The client can **only** send `mode`, `context`, and `text`. There is no
  way to send `model`, `temperature`, `system_prompt`, or a raw `prompt`
  — those are entirely server-controlled; DocPipe's request schema has no
  such fields, so sending them has no effect.

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
    "prompt_version": "v1",
    "model": "qwen2.5:7b-instruct",
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

`model` and `prompt_version` are metadata only, for traceability - never
the prompt text itself, and never Ollama's raw response.

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
- Model and temperature are entirely server-configured
  (`ollama.model`/`ollama.temperature`); no per-client or per-request
  override exists. Temperature defaults to `0` for deterministic
  extraction.
- Timeout: `ollama.timeout_seconds` (default 120s), enforced by DocPipe.
- All of this lives in exactly one place:
  [system/services/ollama.py](system/services/ollama.py). Nothing else in
  the codebase talks HTTP to Ollama or sees its raw response shape.
- Model choice is a config concern, not a code concern: pick any small,
  locally available instruct model that supports Ollama's structured
  outputs — nothing in DocPipe hardcodes a specific model family.

## Files & security

- Uploads are validated by content, not extension: the first bytes must
  be a real PDF signature (`%PDF-`).
- Each upload is written to a temp file with a random name (`tempfile.mkstemp`) —
  the client-supplied filename is never used to build a path, so a
  crafted filename can't cause path traversal.
- The temp file is deleted after the request, on both the success and
  the error path. DocPipe never persists an uploaded document.
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

Never logged: API keys, the `Authorization` header, the Stirling/Ollama
API keys, document contents/text, AI prompts, or AI output.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check system tests
pytest
```

The suite mocks both Stirling and Ollama entirely (`tests/conftest.py`,
`tests/test_ollama_client.py`) and covers: auth (missing/invalid/disabled
key, disabled service for both `documents` and `ai`), file validation
(wrong MIME, oversized upload), the Stirling failure modes (unavailable,
timeout, auth failure), the AI failure modes (unavailable, timeout,
invalid/malformed model output with and without a successful repair,
processing failure), mode registry validation (unknown mode, input too
large, invalid context type), both extraction modes' schemas, a mocked
success path for each, that no secrets/prompts/document text leak into
error responses, and that no temp files are left behind after a request.
No test requires a running Stirling or Ollama instance.

If you do have real Stirling/Ollama instances available locally, a
manual end-to-end check with an actual PDF and a few real documents per
AI mode is worthwhile before deploying, but it is not part of the
automated suite.

## Not implemented (by design)

Semantic search, RAG, model training/fine-tuning, automatic document
classification, a database, quotas/plans/usage/billing, user management,
a web UI, persistent job storage, input chunking for oversized AI text,
and any business logic belonging to a specific consumer project (hotels,
maintenance, contracts, categories, etc.). HubDix or Chronodix may use
DocPipe as a client, but DocPipe has no knowledge of either.
