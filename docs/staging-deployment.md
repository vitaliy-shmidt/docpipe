# Staging deployment guide

This is a deployment/validation guide, not an architecture doc — see the
main [README.md](../README.md) for how DocPipe itself works (API
contract, model profiles, mode routing). This file only covers what to
set up and check when putting DocPipe on a real server for the first
time.

## Services

Three independent processes, typically three containers/hosts:

```text
DocPipe   - the FastAPI service itself (this repo)
Stirling  - PDF text extraction backend (stirlingtools/stirling-pdf)
Ollama    - AI model backend (only needed if any client uses AI analysis)
```

DocPipe is the only one a client application ever talks to directly.
Stirling and Ollama are internal to the DocPipe deployment.

## Configuration

Minimum to decide/set before first deploy:

- **DocPipe client key(s)**: one API key per consuming project
  (`clients.<id>.api_key` in `config.yaml`), generated with real entropy
  (not a copy of any example value).
- **`services.documents`** / **`services.ai`** per client: only enable
  `ai` for a client that's actually meant to use `/documents/analyze`.
- **Stirling URL**: `stirling.base_url`, reachable from the DocPipe host/container.
- **Ollama URL**: `ollama.base_url`, reachable from the DocPipe host/container -
  only required if at least one client has `services.ai: true`.
- **Model profiles**: `models.light` / `models.standard` (at minimum -
  these are the two currently-active modes' defaults; `models.heavy` is
  optional until a mode that defaults to it exists) - each pointing at a
  model actually pulled into that Ollama instance.
- **Mode routing / overrides**: confirm which client gets which profile
  per mode (`clients.<id>.model_overrides`, or the mode defaults if none
  is set) - see README.md "Model profiles" for the exact resolution order.
- **OCR fallback** (`stirling.ocr`): on by default - confirm the target
  Stirling instance actually has OCR tools available (OCRmyPDF and/or
  Tesseract) and the language data for every code listed in
  `stirling.ocr.languages` (default `deu`+`eng`). If Stirling has neither
  tool installed, OCR attempts fail with the existing
  `upstream_unavailable`/`processing_failed` codes - not a new failure
  mode, but worth confirming once rather than discovering it on the first
  real scanned document. See README.md "Text extraction" for the full
  fallback flow and `stirling.ocr.min_meaningful_characters` threshold.

Config is validated at startup (see README.md "Config validation") - a
broken model profile or routing reference makes the process refuse to
start, so a failed deploy here should show up immediately in the startup
logs, not as a mysterious first-request 500.

## Server validation

After deployment, run through these in order:

```text
GET  /api/v1/health                       - unauthenticated, confirms the process is up
GET  /api/v1/capabilities                 - authenticated, confirms a real client key resolves
                                             correctly and reports the services/modes that
                                             client actually has
POST /api/v1/documents/extract-text       - a real, text-based PDF, confirms the Stirling
                                             fast path end-to-end (expect
                                             extraction_method="embedded_text")
POST /api/v1/documents/extract-text       - a real, scanned/image-only PDF, confirms the OCR
                                             fallback end-to-end (expect
                                             extraction_method="ocr" and meaningfully more than
                                             a handful of characters back - not just the ~3
                                             characters a scan yields without OCR)
POST /api/v1/documents/analyze            - a real (or synthetic) text, confirms the Ollama
                                             path end-to-end for each model profile in use
```

Also worth a deliberate negative check: call `/api/v1/capabilities` (or
`/analyze`) with a wrong/missing key and confirm you get `401
unauthorized` with no internal detail in the response body.

The OCR fallback call naturally takes noticeably longer than the plain
extraction call (full-page rendering + Tesseract/OCRmyPDF per page, not
just a text-layer read) - note both durations when validating (see
"RAM awareness" below for why that matters on an 8 GB box) rather than
treating the slower response as a problem on its own.

## Staging checklist

```text
[ ] DocPipe erreichbar
[ ] HTTPS / Reverse Proxy
[ ] API Key funktioniert
[ ] unauthorized funktioniert
[ ] Stirling erreichbar
[ ] PDF Extraction funktioniert (textbasiertes PDF, embedded_text)
[ ] OCR Fallback funktioniert (gescanntes PDF, extraction_method=ocr)
[ ] Ollama erreichbar
[ ] light Profile funktioniert
[ ] standard Profile funktioniert
[ ] Schema Validation funktioniert
[ ] Logs enthalten keine Dokumentinhalte
[ ] RAM gemessen
[ ] CPU gemessen
[ ] Laufzeit gemessen
```

## RAM awareness

The target server has roughly **8 GB RAM total**. DocPipe itself is
lightweight; the real budget pressure is:

```text
Stirling RAM  +  Ollama model RAM  +  DocPipe RAM  +  OS/other processes
```

A loaded Ollama model can easily be the single largest consumer on the
box (a 1.5B-parameter instruct model already uses roughly 1-2 GB
resident; a `standard`/`heavy` profile pointed at a larger model will use
correspondingly more). This pass does **not** implement any model
unload/eviction logic - Ollama's own default behavior (keeping a model
loaded in memory for a period after last use) applies as-is. Before
assigning a `standard` or `heavy` profile in production, measure actual
resident memory for that specific model on the actual target hardware -
don't assume it fits from a parameter count alone. If both `light` and
`standard` point at genuinely different model files and both may be
invoked in the same time window, budget for both being resident
simultaneously, not just the larger of the two.

The OCR fallback adds its own transient RAM cost on the Stirling side
(page rasterization + Tesseract/OCRmyPDF) for the duration of a single
OCR call - check `docker stats` for Stirling specifically during an OCR
staging test, not just at idle. The AI model does not need to be loaded
for an OCR test, so OCR and AI load can be measured independently rather
than assumed additive.

## Parallelism

Staging validation should assume **one AI request at a time** and load
test accordingly (serially, one request completing before the next
starts) - there is no request queue, worker pool, or concurrency limiter
in this pass, by design (out of scope, see README.md "Not implemented").
If the deployed Ollama/DocPipe combination happens to accept concurrent
requests without falling over, that's not something to rely on yet or to
artificially prevent either - just don't design a staging load test
around concurrent AI calls until this has been deliberately revisited.

## What's already covered vs. what only staging can confirm

**Already validated in this repo** (automated tests + local QA against a
real Stirling and a real Ollama instance in this pass):

- the full extract-text -> Stirling round trip with a real PDF;
- the full analyze -> Ollama round trip with a real small instruct model
  (`qwen2.5:1.5b-instruct`) across both active modes, structured output,
  schema validation, and the null/no-hallucination prompt rules;
- config fail-fast validation, mode/client resolution order, and the
  strict request schema (client cannot influence model choice).

**Only the real staging server can confirm:**

- actual RAM/CPU headroom with the *actual* models chosen for
  `light`/`standard` (this pass's local QA used one small model shared
  across both profiles - a real deployment should measure its own,
  possibly larger, `standard`/`heavy` choices);
- reverse proxy / HTTPS termination behavior;
- realistic request latency under the target network topology;
- behavior under whatever concurrency the target actually receives;
- that Stirling's actual OCR tooling (OCRmyPDF/Tesseract) and language
  data for every configured `stirling.ocr.languages` code are really
  present on that instance - the automated test suite only exercises
  DocPipe's side of the OCR contract against a mock.

## Troubleshooting notes from real staging runs

**Duplicate Stirling API key →
`IncorrectResultSizeDataAccessException: Query did not return a unique
result: 2 results were returned`.** Seen when the same API key value had
been created twice inside Stirling's own user/key store (e.g. re-running
a setup step). Stirling's own key lookup then fails non-deterministically
for any request authenticated with that key, surfacing to DocPipe as an
upstream error. Fix: remove the duplicate key entry on the Stirling side
and issue a fresh one; confirm with a plain `/api/v1/convert/pdf/text`
call returning `200` before assuming an OCR-specific problem. This is a
Stirling-side data issue, not anything DocPipe's code does differently
per key - worth checking first if *any* Stirling-backed call (not just
OCR) starts failing after a key rotation.
