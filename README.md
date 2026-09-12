# DocPipe

Generic, authenticated document processing proxy.

```text
Client Application
      |
      v
   DocPipe  --  API key check
      |
      v
   Stirling
      |
      v
   Response normalization
      |
      v
Client Application
```

Later, without changing this contract:

```text
DocPipe
├─ Stirling      (document processing, today)
└─ AI Provider   (not implemented yet)
```

DocPipe's job is to be a stable boundary: client applications (HubDix,
Chronodix, or anyone else) never need to know which document processing
engine sits behind it, or how to talk to it.

## What can V1 do?

- API key authentication (`Authorization: Bearer <key>`)
- PDF text extraction, proxied through [Stirling-PDF](https://github.com/Stirling-Tools/Stirling-PDF)
- A normalized, stable success/error response contract
- Per-client service flags loaded from a local config file (no database)

## What can V1 NOT do?

- No AI / no Ollama integration
- No OCR orchestration beyond what Stirling itself provides
- No database, no persistent jobs, no background queue
- No quotas, plans, usage tracking, or billing
- No user management, no web UI
- No persistence of uploaded documents — every request is processed
  in a temp file that is deleted immediately afterwards
- No business logic specific to any consuming project (HubDix or otherwise)

## Architecture

- **Framework**: Python + FastAPI. Small, REST-friendly, built-in OpenAPI
  docs (`/docs`), and a natural fit for adding an AI/Ollama path later
  without a rewrite.
- **Version**: `0.1.0`, held in one place ([system/__init__.py](system/__init__.py)) and reused
  everywhere else (health response, FastAPI app metadata).

```text
docpipe/
├─ system/                  # application source root
│  ├─ main.py                # app wiring, middleware, error handlers
│  ├─ config.py               # YAML config + env overrides
│  ├─ auth.py                  # API key authentication
│  ├─ errors.py                 # normalized error codes
│  ├─ schemas.py                 # response models
│  ├─ services/
│  │  └─ stirling.py              # the only module that knows Stirling
│  └─ routes/
│     ├─ health.py
│     ├─ capabilities.py
│     └─ documents.py
├─ tests/                    # pytest suite, Stirling is mocked
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

pip install -r requirements.txt
uvicorn system.main:app --host 0.0.0.0 --port 8000
```

Or with Docker Compose (spins up DocPipe + a local Stirling instance):

```bash
cp config.example.yaml config.yaml
docker compose up --build
```

Stirling doesn't have to run in the same Compose stack — point
`stirling.base_url` (or `STIRLING_URL`) at any existing instance instead;
Compose here is convenience only.

### Try it

```bash
curl http://localhost:8000/api/v1/health

curl -H "Authorization: Bearer YOUR_KEY" \
  http://localhost:8000/api/v1/capabilities

curl -X POST \
  -H "Authorization: Bearer YOUR_KEY" \
  -F "file=@sample.pdf" \
  http://localhost:8000/api/v1/documents/extract-text
```

Interactive API docs are available at `/docs` once the server is running.

## Configuration

No database. Clients and their permissions live in a local YAML file:

```yaml
server:
  max_file_size_mb: 25   # technical safety limit, not a quota

stirling:
  base_url: "http://stirling:8080"
  api_key: ""
  timeout_seconds: 90

clients:
  demo-client:
    enabled: true
    api_key: "change-me"
    services:
      documents: true
```

Only `config.example.yaml` is committed. The real `config.yaml` (or
whatever `DOCPIPE_CONFIG` points at) must stay out of git and should have
restrictive file permissions (`chmod 600` on the host, or a mounted
read-only secret in your orchestrator) since it holds client API keys in
plain text — deliberately, to keep V1 administration simple. Keys are
never logged, never echoed back in error responses, and the config
content is never exposed through the API.

### Environment overrides

| Variable            | Overrides                    | Default        |
|---------------------|-------------------------------|----------------|
| `DOCPIPE_CONFIG`    | path to the YAML config file   | `config.yaml`  |
| `STIRLING_URL`      | `stirling.base_url`             | (from file)    |
| `STIRLING_API_KEY`  | `stirling.api_key`               | (from file)    |
| `STIRLING_TIMEOUT`  | `stirling.timeout_seconds`        | (from file)    |

Priority: environment variable > value in the YAML file > built-in default.
`DOCPIPE_CONFIG` only selects *which* file is read; it does not override a
value inside it.

Client definitions (`clients:`) are only ever read from the YAML file —
there is intentionally no per-client environment override.

## API

All authenticated endpoints expect:

```text
Authorization: Bearer <api-key>
```

### `GET /api/v1/health` — unauthenticated

Confirms the DocPipe process itself is up. Does not contact Stirling.

```json
{ "ok": true, "service": "docpipe", "version": "0.1.0" }
```

### `GET /api/v1/capabilities` — authenticated

Reflects what the calling client is actually allowed to use.

```json
{
  "ok": true,
  "services": { "documents": true, "ai": false },
  "features": ["extract_text"]
}
```

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

### Error contract

Every error, from any endpoint, has the same shape:

```json
{ "ok": false, "code": "upstream_unavailable", "message": "Document processing service is temporarily unavailable." }
```

| code                    | HTTP status | Meaning                                      |
|-------------------------|-------------|-----------------------------------------------|
| `unauthorized`          | 401         | API key missing, unknown, or client disabled  |
| `service_disabled`      | 403         | Client is known but `documents` isn't enabled |
| `invalid_file`          | 400         | Missing file, empty file, or not a real PDF   |
| `file_too_large`        | 413         | Upload exceeds `server.max_file_size_mb`      |
| `upstream_unavailable`  | 502         | Stirling unreachable or returned a 5xx        |
| `upstream_auth_failed`  | 502         | Stirling rejected DocPipe's own credentials   |
| `processing_failed`     | 502         | Stirling reachable but returned another error |
| `timeout`               | 504         | Stirling did not respond within the timeout   |
| `internal_error`        | 500         | Unexpected DocPipe-side failure               |

No stack traces, no upstream secrets, and no raw Stirling payloads ever
reach the client — that boundary is one of DocPipe's main jobs.

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

DOC/DOCX extraction is intentionally not exposed in V1: only PDF text
extraction has been verified against the current Stirling API.

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
- No retries: if Stirling is unavailable, DocPipe answers immediately
  with `upstream_unavailable`. Retrying is left to the calling client.
- DocPipe is stateless — no DB, no queue, no persisted jobs, no usage
  tracking. Request in, response out.

## Logging

Structured, one line per request: timestamp, request ID, client ID,
endpoint, HTTP status, duration. A `X-Request-ID` response header is set
on every response for correlating client-side and server-side logs.

Never logged: API keys, the `Authorization` header, the Stirling API key,
or document contents/text.

## Tests

```bash
pip install -r requirements-dev.txt
ruff check system tests
pytest
```

The suite mocks Stirling entirely (`tests/conftest.py`) and covers auth
(missing/invalid/disabled key, disabled service), file validation
(wrong MIME, oversized upload), the Stirling failure modes
(unavailable, timeout, auth failure), a mocked success path, and that no
temp files are left behind after a request. No test requires a running
Stirling instance.

If you do have a real Stirling instance available locally, a manual
end-to-end check with an actual PDF is worthwhile before deploying, but
it is not part of the automated suite.

## Not implemented (by design)

AI/Ollama integration, OCR orchestration beyond Stirling, a database,
quotas/plans/usage/billing, user management, a web UI, persistent job
storage, and any business logic belonging to a specific consumer project
(hotels, maintenance, contracts, categories, etc.). HubDix or Chronodix
may use DocPipe as a client, but DocPipe has no knowledge of either.
