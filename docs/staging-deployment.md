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
- **Prompt Lab runtime directory** (`prompts.runtime_dir`, V2.2): must be
  a *writable*, persistent volume mount, separate from the read-only
  `system/prompts` bind mount - see "Prompt Lab runtime directory" below.
  Only relevant if any client has `services.prompt_lab: true`.
- **`services.prompt_lab`** / **`services.assistant`** per client (V2.2):
  both independent opt-ins from `ai`, like `services.ai` itself - a
  client needs the matching flag even if it already has `ai: true`. In
  practice `prompt_lab` should normally go only to an internal/admin
  client (e.g. HubDix's DocLab), not an ordinary integration client - see
  [docs/prompt-lab.md](../docs/prompt-lab.md).

Config is validated at startup (see README.md "Config validation") - a
broken model profile or routing reference (extraction **or** assistant
mode routing, V2.2) makes the process refuse to start, so a failed deploy
here should show up immediately in the startup logs, not as a mysterious
first-request 500. The same startup check also fails fast if a base
prompt file itself is missing/unreadable (e.g. a botched image build) -
this covers both extraction and assistant modes.

## Prompt Lab runtime directory (V2.2)

`prompts.runtime_dir` (default `runtime-prompts`, relative to the process
working directory - `/app/runtime-prompts` inside the container) is where
a `prompt_lab` client's saved versions and each mode's active-version
pointer actually live. It is **not** the same mount as
`system/prompts` (read-only, baked into the image) - see
[docs/prompt-lab.md](../docs/prompt-lab.md) "Base vs. runtime prompts".

```yaml
# docker-compose.yml (already present in this repo)
volumes:
  - ./config.yaml:/app/config.yaml:ro
  - ./runtime-prompts:/app/runtime-prompts
```

Before first deploy:

- create the host-side `./runtime-prompts` directory yourself (don't let
  Docker auto-create it) and confirm it's writable by the UID the
  container actually runs as - the image runs as a non-root user
  (`docpipe`, uid `1000`, see `Dockerfile`), so a bind-mounted directory
  owned by a different UID/root-only permissions will make every Lab
  save/activate fail with a filesystem permission error, not a clean
  DocPipe error code;
- it does not need to be pre-populated - `PromptRegistry` creates
  `runtime-prompts/<mode>/` on the very first save for that mode;
- back it up like any other stateful volume if Lab-saved prompt versions
  matter to you - losing it doesn't break DocPipe (every mode falls back
  to its repo-shipped base version), it just silently reverts every
  mode to that base version.

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

If any client has `services.prompt_lab: true` (V2.2), run the **prompt
hot-reload acceptance test** - this is the central Prompt Lab check, see
[docs/prompt-lab.md](../docs/prompt-lab.md):

```text
1. POST /documents/analyze (maintenance_extraction) - note prompt_version in the response (normally "v1")
2. POST /lab/prompts/maintenance_extraction/versions - save a trivially different draft as "v2"
3. POST /lab/prompts/maintenance_extraction/activate - {"version": "v2"}
4. POST /documents/analyze (maintenance_extraction) again - prompt_version must now read "v2"
```

No DocPipe restart/redeploy anywhere between steps 1 and 4 - if step 4
still reports `v1`, something is wrong with the runtime directory (wrong
mount, permission error silently swallowed, wrong `prompts.runtime_dir`
in `config.yaml`), not with the Lab logic itself (already covered by
`tests/test_lab.py::test_prompt_hot_reload_without_restart`, which
exercises the identical sequence against a mocked Ollama).

If any client has `services.assistant: true` (V2.2), a quick smoke test:

```json
POST /api/v1/assistant/query
{ "question": "Wie steht mein Hotel technisch da?", "context": { "maintenance_overdue": 2, "open_defects": 3, "critical_defects": 1 } }
```

Expect `assistant_mode: "hotel_health_summary"` and an `answer` that only
mentions the numbers actually supplied in `context` - no invented
details, no claim of a fact `context` doesn't contain. See
[docs/assistant-routing.md](../docs/assistant-routing.md).

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
[ ] runtime-prompts/ beschreibbar (richtiger UID/Mount) - nur falls prompt_lab genutzt wird
[ ] Prompt Hot Reload funktioniert ohne Neustart - nur falls prompt_lab genutzt wird
[ ] Assistant Smoke Test liefert nur belegte Fakten - nur falls assistant genutzt wird
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

**Prompt Lab / assistant routing (V2.2) specifically:** this pass's
verification was entirely the automated test suite
(`tests/test_prompt_registry.py`, `tests/test_lab.py`,
`tests/test_assistant_router.py`, `tests/test_assistant.py` - 179 tests
total, all against a mocked Ollama) - no real Ollama instance or
container redeploy was available to confirm the hot-reload acceptance
test and the assistant smoke test above against a real model. Both are
called out explicitly in the staging checklist because they're the one
thing the mock cannot stand in for: the mocked hot-reload test proves the
*registry/routing* logic is correct, but only a real
`docker compose up -d` with an actual mounted `runtime-prompts` volume
proves the *deployment* (mount, permissions, `config.yaml` path) is
correct too.

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
