# Prompt Lab

Runtime-resolved, versioned prompts for each extraction mode, plus a
`prompt_lab`-gated API to inspect, draft-test, save, and activate them -
so a prompt change is a config-like operation (save a version, activate
it) instead of a code change requiring an image rebuild/redeploy. See
`system/ai/prompt_registry.py` for the implementation and
`system/routes/lab.py` for the API.

Not covered here: `docs/assistant-routing.md` documents the separate
assistant routing foundation, which reuses this same registry for its own
(not yet Lab-editable) prompts.

## Base vs. runtime prompts

```text
system/prompts/<mode>/<version>.txt     <- base, repo-shipped, read-only in production
runtime-prompts/<mode>/<version>.txt    <- runtime, writable, Lab-saved versions
runtime-prompts/<mode>/active.json      <- runtime, writable, {"version": "..."}
```

Every lookup checks `runtime_dir` first, then falls back to `base_dir`
(`system/ai/prompt_registry.py`'s `PromptRegistry`). This is why a mode's
original `v1` still resolves even though it only ever lives in the base
directory - only versions actually saved through the Lab get a runtime
file. `active.json` always lives in `runtime_dir`, even when it points at
a base-only version: activation has to be writable regardless of where
the *content* of the activated version happens to live, and the base
directory is never written to.

A version is **immutable** once it exists in either layer: saving `v1`
again (even with different content) is rejected exactly like saving an
already-saved `v2` - see "Errors" below.

## Runtime prompt storage / mount

```yaml
# docker-compose.yml
volumes:
  - ./config.yaml:/app/config.yaml:ro
  - ./runtime-prompts:/app/runtime-prompts
```

The base directory needs no explicit mount - it's baked into the image
build (`Dockerfile`'s `COPY system ./system`) and DocPipe never writes to
it. The runtime directory is a second, writable, persistent volume -
without it, Lab saves/activations still work inside a running container
but are lost on the next rebuild/redeploy. It does not need to
pre-exist or be pre-populated: `PromptRegistry` creates
`runtime-prompts/<mode>/` on the first save for that mode.

Config (`config.yaml`, all optional - shown values are the defaults):

```yaml
prompts:
  base_dir: "system/prompts"
  runtime_dir: "runtime-prompts"
  max_content_length: 32000
```

## No caching, by design

`PromptRegistry` reads straight from disk on every single call - no
in-process cache, no TTL, nothing to invalidate. For this deployment's
scale, a filesystem read per request is a deliberately simple, sufficient
choice (see the task this was built from: "für diese kleine Installation
ist direktes Lesen vom Datenträger pro Request akzeptabel"). The direct
upside: activating a new version takes effect on the *very next* request
- no cache-busting logic, no container restart, nothing else to
coordinate. `tests/test_prompt_registry.py::test_hot_reload_without_restart`
and `tests/test_lab.py::test_prompt_hot_reload_without_restart` both
exercise exactly this.

## Version naming

`v1`, `v2`, ..., `v10`, ... - no leading zeros, nothing else
(`system/ai/prompt_registry.py::is_valid_version_name`, pattern
`^v[1-9][0-9]*$`). This alone rejects `../../foo`, `test.txt`, and
`v1/abc` without a separate path-traversal-specific check - there is no
code path anywhere in the registry that turns raw client input directly
into a filesystem path without first passing this check (or, for the
mode/subdir segment, being looked up against the fixed `MODES`/
`ASSISTANT_MODES` registries - see `system/routes/lab.py::_get_known_mode`).

## API (all under `/api/v1/lab/`, `prompt_lab` client permission required)

| Endpoint | Method | Needs `ai` too? | Purpose |
|---|---|---|---|
| `/lab/prompts` | GET | no | list every mode's active version + all known versions |
| `/lab/prompts/{mode}` | GET | no | load a version's content (`?version=` optional, defaults to active) |
| `/lab/analyze` | POST | **yes** | test a *draft* prompt - the only Lab route that calls Ollama |
| `/lab/prompts/{mode}/versions` | POST | no | save a new, immutable version |
| `/lab/prompts/{mode}/activate` | POST | no | make an existing version the active one |

`mode` is always validated against the same `system/ai/modes.py` `MODES`
registry `/documents/analyze` uses - an unknown mode is the same
`unknown_mode` error either endpoint would give.

### Draft analyze - the important one

```json
POST /api/v1/lab/analyze
{ "mode": "maintenance_extraction", "text": "...", "context": {}, "prompt": "temporary draft..." }
```

- Uses the request's `prompt` **only** - never the mode's active version,
  never anything persisted. `system/routes/lab.py::draft_analyze` builds
  the final prompt straight from `body.prompt`, full stop.
- Never persists anything - saving is `/lab/prompts/{mode}/versions`, a
  separate, explicit call.
- Same model routing as `/documents/analyze`
  (`system/ai/resolver.py::resolve_model_profile`, unchanged, no
  Lab-specific model override), same response JSON Schema, same
  single-repair-attempt mechanism
  (`OllamaClient.generate_structured()`, unchanged, called identically).
  A draft is tested with the exact same mechanics a real request would
  use - only the prompt text differs.

```json
{
  "ok": true,
  "data": {
    "mode": "maintenance_extraction",
    "prompt_source": "draft",
    "prompt_sha256": "…",
    "model_profile": "light",
    "model": "qwen2.5:1.5b-instruct",
    "result": { "...": "..." }
  }
}
```

`prompt_sha256` is additive metadata for later reproducibility (which
exact draft text produced this result) - never a substitute for an
actual saved version.

**`/documents/analyze` itself never accepts a `prompt` field** - it
already rejects any request body it doesn't recognize
(`AnalyzeRequest`'s `extra="forbid"`), and that boundary is unchanged by
this pass. A draft prompt only ever exists inside a single
`/lab/analyze` call.

### Save + activate - separate, deliberate steps

```json
POST /api/v1/lab/prompts/maintenance_extraction/versions
{ "version": "v2", "content": "..." }
```

Rejects: an empty/whitespace-only body, content over
`prompts.max_content_length`, a version name that fails the naming check
above, a version that already resolves (runtime **or** base - see
"Base vs. runtime prompts"), and a template that doesn't actually render
(a stray `{`/`}` not part of a valid `{context_block}`/`{text}`
placeholder - checked with the exact same `.format()` call
`build_prompt()` uses, so a syntax error is caught at save time, not on
some later real request).

**Saving never activates.** `active_version` for the mode is unchanged
until a separate, explicit call:

```json
POST /api/v1/lab/prompts/maintenance_extraction/activate
{ "version": "v2" }
```

Rejects a version that doesn't resolve (`unknown_prompt_version`, 404) -
there is no way to activate something that was never saved (or isn't the
base version).

## Errors

| code | HTTP status | Meaning |
|---|---|---|
| `prompt_lab_disabled` | 403 | client is known but `prompt_lab` isn't enabled |
| `unknown_prompt_version` | 404 | the requested/activated version doesn't resolve (runtime or base) |
| `invalid_prompt_version` | 400 | malformed version name on save (see naming rules) |
| `prompt_version_exists` | 409 | save target already exists - versions are immutable |
| `invalid_prompt` | 400 | empty content, or a template that fails to render |
| `prompt_too_large` | 413 | content (save) or draft prompt (`/lab/analyze`) over the configured max |

All reuse the same top-level `{"ok": false, "code": ..., "message": ...}`
shape as every other DocPipe error - no separate Lab-specific response
format.

## Logging

One line per Lab call: `request_id`, `client_id`, `action`
(`list`/`load`/`draft_analyze`/`save_version`/`activate`), `mode`,
`version` (where applicable). Never the prompt content, the document
text, or the draft result - identical boundary to `/documents/analyze`'s
existing logging.

## Not implemented

- Prompt schema editing (`schema.json` stays code/git-only - see the
  task: "Schema-Änderungen bleiben Code-/Git-Änderungen").
- Per-client prompt version pinning/override (every client sees the same
  active version for a mode).
- Version deletion/rollback beyond re-activating an older version.
- A UI of its own inside DocPipe - HubDix's DocLab is the intended
  consumer (see `docs/staging-deployment.md` and HubDix's own
  `docs/modules/doclab.md`).
