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
POST /api/v1/documents/extract-text       - a real PDF, confirms the Stirling path end-to-end
POST /api/v1/documents/analyze            - a real (or synthetic) text, confirms the Ollama
                                             path end-to-end for each model profile in use
```

Also worth a deliberate negative check: call `/api/v1/capabilities` (or
`/analyze`) with a wrong/missing key and confirm you get `401
unauthorized` with no internal detail in the response body.

## Staging checklist

```text
[ ] DocPipe erreichbar
[ ] HTTPS / Reverse Proxy
[ ] API Key funktioniert
[ ] unauthorized funktioniert
[ ] Stirling erreichbar
[ ] PDF Extraction funktioniert
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
- behavior under whatever concurrency the target actually receives.
