# Performance findings and optimization roadmap

Status: **2026-11-04**

This document records the current real-server findings for DocPipe on the
HubDix staging deployment and the next optimization direction. It is not a
new architecture: the existing boundaries remain unchanged.

Related:

- [Staging deployment](staging-deployment.md)
- [Contract text optimization](contract-text-optimization.md)
- [Assistant routing](assistant-routing.md)
- [Prompt Lab](prompt-lab.md)

## Current deployment snapshot

The current measured HubDix staging host is CPU-only:

| Resource | Measured deployment |
|---|---|
| OS | Ubuntu 24.04 LTS |
| CPU | 8 vCPU, AMD EPYC Milan class |
| RAM | ~15 GiB |
| GPU | none available to Ollama |
| Swap | 8 GiB |
| Ollama | host service, loopback only |
| Stirling-PDF | container, loopback only |
| DocPipe | container, loopback only |

The public host address and all API keys/secrets are deliberately excluded
from repository documentation.

Current measured service topology:

```text
client / reverse proxy
        |
        v
DocPipe 127.0.0.1:8000
   |                  |
   v                  v
Stirling          Ollama
127.0.0.1:8080    127.0.0.1:11434
```

The deployment currently uses host networking for DocPipe because both
backends are intentionally bound to host loopback. Uvicorn itself is also
bound to `127.0.0.1:8000`; do not replace that with a public
`0.0.0.0:8000` listener unless a deliberate network/security design
requires it.

## Current model split

This is an environment-specific staging choice, not a DocPipe product
default:

| Profile | Current model | Purpose |
|---|---|---|
| `light` | `qwen2.5:0.5b-instruct` | fast/simple tasks |
| `standard` | `qwen2.5:0.5b-instruct` | interactive assistant / cross-domain in current staging |
| `heavy` | `qwen2.5:1.5b-instruct` | contract extraction |

HubDix currently overrides `contract_extraction` to `heavy`. The
`heavy` profile uses an 8,192-token context window in this deployment.

This split must not become a hardcoded DocPipe assumption. Profiles remain
generic resource classes and concrete model names remain configuration.

## Real latency findings

These are observations from the current CPU-only staging host, **not an SLA**.

### qwen3:4b

A previous test used `qwen3:4b`.

- warm-up: roughly 5.6 s;
- a normal assistant request with ~3.8k context characters exceeded the
  90 s light-profile timeout.

Conclusion: useful quality, but too slow for the current interactive CPU-only
deployment.

### qwen2.5:1.5b-instruct at 4,096 context

Observed examples:

- `hotel_health_summary`: ~55 s;
- short contract (~1,227 input characters), one AI call: ~32.5 s;
- contract (~17,168 input characters), optimizer produced ~8,122 characters,
  3 chunks / 3 AI calls: ~77-98 s.

The dominant cost is Ollama CPU inference, not FastAPI/DocPipe execution.

### qwen2.5:0.5b-instruct for assistant

Observed examples:

- `hotel_health_summary`: ~3,837 context characters + 6 history turns,
  ~26.7 s;
- `cross_domain_question`: ~2,639 context characters + no history,
  ~11.9 s.

Conclusion: model size helps, but context and history size still have a
material latency cost.

### 8,192 context for contract extraction

For the same ~17,168-character contract, moving the heavy profile to
`context_window_tokens: 8192` changed the planner result to:

```text
normalized_chars  ~16,482
optimized_chars   ~6,952
strategy          relevance
chunk_count       0
ai_calls          1
total             ~64.7 s
```

The previous 4,096-context run used three calls and reached up to ~98 s.

Conclusion: **reducing AI-call count can matter more than aggressively
reducing the character target**.

### Failed experiment: target_chars = 5,500

Reducing `target_chars` from the normal ~7,000 range to 5,500 caused the
same contract to become chunked again:

```text
strategy          chunked
optimized_chars   ~12,118
chunk_count       3
ai_calls          3
total             ~107 s
```

This is expected from the current planner semantics: `target_chars` is not
a blind truncation limit. If all significant passages no longer fit the
single-call target, the planner selects the chunked strategy.

Decision: do not treat "smaller target" as automatically faster.

### Large real KONE contract

One real contract produced:

```text
extracted         78,686 characters
Stirling extract  ~1.2 s
normalized        76,880
optimized         13,045
strategy          chunked
chunk_count       3
ai_calls          3
total             ~83-112 s
```

Important conclusions:

- embedded text extraction is not the bottleneck in this case;
- input cleanup, relevance selection, prompt overhead and AI-call count are
  the remaining software-side levers;
- quality regression must be checked against the real fields that matter:
  provider, contract number/type, service scope, dates, notice, renewal,
  amount/currency/payment interval and notes.

## Optimization decision

For the next pass, keep the Ollama/model setup stable and optimize **before
the model**.

Priority order:

1. transient text normalization V2 (**done 2026-11-04**, see below);
2. `contract_extraction` prompt V2 (**done 2026-11-04** as a clarification
   pass; the compact restructure is still open, see below);
3. expose Ollama metrics in safe logs (**token counts done 2026-11-04**:
   `prompt_eval_count`/`eval_count`; phase durations still only
   per-call internally);
4. Planner V2 - split mandatory passages from additional context and decide
   against the real prompt/context budget (**implemented 2026-11-04**; it
   **regressed on the real KONE contract**, so Planner V2.1 bounds the
   mandatory set by field coverage, **2026-11-05**; staging validation of
   V2.1 open, see below);
5. reduce caller-supplied assistant context/history;
6. only after those measurements revisit model/hardware tuning.

## Text normalization V2

The existing optimizer is intentionally conservative and must remain
deterministic. The next pass should improve technical cleanup without
summarizing or interpreting document content.

Candidate improvements:

- stronger collapse of redundant blank lines;
- safer rejoining of PDF line wraps into paragraphs;
- more robust OCR hyphenation repair;
- stronger repeated header/footer detection;
- conservative near-duplicate OCR line/paragraph removal;
- additional obvious layout/symbol artifact removal;
- preserve tables and short business-significant lines.

Examples that must **never** be discarded just because they are short:

```text
3 Monate
6.999,00 €
§ 4
KONE GmbH
```

The persisted consumer-side `extracted_text` must remain untouched. The
cleaned representation exists only for the lifetime of the AI request.

### Status: implemented (2026-11-04)

Rules, safety guarantees and limits:
[contract-text-optimization.md, "Phase A"](contract-text-optimization.md#phase-a---normalization-normalizepy-v2).
No configuration option was added, the heuristics live in code. Diagnostics are
unchanged: `raw_chars`/`normalized_chars` already show the effect.

Synthetic measurements (`tests/contract_samples.py`, local dev machine,
normalization only):

| sample | raw | V1 normalized | V2 normalized | line breaks V1 → V2 | V2 time |
|---|---|---|---|---|---|
| plain text, 17k | 18,473 | 18,003 | 18,003 | 69 → 69 | ~10 ms |
| plain text, 86k | 86,434 | 83,528 | 83,528 | 195 → 195 | ~50 ms |
| PDF layout, 17k | 18,431 | 16,349* | 17,187 | 174 → 88 | ~15 ms |
| PDF layout, 53k | 52,984 | 46,153* | 49,432 | 409 → 175 | ~40 ms |
| PDF layout, 110k | 109,853 | 91,021* | 102,827 | 799 → 333 | ~90 ms |

"PDF layout" = hard-wrapped at ~78 chars, 3-line letterhead + rule + page
counter on every page, TOC, tabs/NBSP, an OCR double line.

\* V1's smaller numbers include **lost body text**: its spacing-based header
rule deleted recurring wrapped body lines (≈4 % of the words in these
samples). V2 removes only ballast (letterheads, footers, page counters, rule
lines, TOC, the OCR double line): 17k sample ≈ −6.8 % characters, and every
clause stays.

Expected benefit on real contracts:

- **Character reduction stays modest.** The real KONE text lost only about
  2.3 % in V1 normalization, so most of it is content. TOC runs and
  multi-line letterheads/footers are the main additional savings.
- **Cleaner prompts.** Roughly half the line breaks go away in wrapped PDF
  text, which means fewer tokens than the character estimate suggests.
  Sentences that cross pages stay whole, so a clause lands in one block.
- **The TOC no longer counts as a significant hit.** It used to match every
  clause keyword and crowd the document head.
- **Fewer silent losses.** Business values and body lines are no longer
  dropped (see the V1 fixes in the linked doc).

Not changed: planner thresholds, `target_chars`, prompt v1, models, Ollama
settings. `optimized_chars` therefore stays in the same ~6-7k range at an
8,192 window: the relevance selection still fills its target, so cleaner
input means more relevant text per call, not a smaller call.

### Observations for the next passes

Contract prompt V2:

- the excerpt markers (`[Excerpt 2/4]`) are the only non-document text in
  `{text}`. V2 should explain them in a single sentence instead of the
  model guessing;
- normalized text keeps table column gaps as two spaces or `|`. A short
  hint that two-space gaps separate table columns may help price/interval
  extraction.

Planner:

- the planner fills `target_chars` regardless of how clean the input is.
  To turn cleaner input into fewer **tokens**, the planner pass has to
  decide on must-have vs. nice-to-have context (see below), not the
  normalizer;
- measure the line-break/token effect directly via Ollama
  `prompt_eval_count`. The character-based estimate (3.2 chars/token)
  cannot see it;
- digit-initial continuations (`… beträgt`⏎`3 Monate`) are not joined and
  can split a clause across two blocks. The small-neighbor rule usually
  keeps them together; check that against real texts.

## Contract prompt V2

The current v1 prompt is safe but verbose. Several global constraints are
repeated in individual field descriptions even though the response schema is
also sent to Ollama as `format`.

A v2 should reduce redundancy while keeping all safety semantics.

Suggested shape:

```text
ROLE
GLOBAL RULES
FIELD-SPECIFIC EXCEPTIONS
CONTEXT
DOCUMENT TEXT
```

Global rules should appear once:

- use only explicit document evidence;
- use `null` for absent/ambiguous evidence;
- never calculate missing dates/terms;
- never perform legal interpretation;
- context can disambiguate but is never evidence;
- no internal IDs;
- JSON only.

Keep field-specific wording only where it changes semantics, e.g. provider vs
customer, notice/renewal wording, one-unambiguous-price rule and contradiction
handling.

Do not overwrite v1. Add v2 and compare both through Prompt Lab against the
same real contracts before activation.

### Status (2026-11-04): v2 = clarification pass, compaction still open

The shipped `v2` is deliberately **not** the compact restructure above. It
is v1 plus three clarifications that match what the optimizer actually
sends:

- `[Excerpt n/m]` blocks are parts of the same contract;
- several spaces or `|` may separate table columns;
- contradictions across excerpts are not reconciled.

Details are in [contract-text-optimization.md, "Prompt v2"](contract-text-optimization.md#prompt-v2).

- **Default:** `v2` is the default. The diff to v1 is two additive lines,
  and rollback is one Prompt Lab activation of `v1`. The real-contract
  v1/v2 comparison is still to be done on staging.
- **Cost:** +333 chars (~100 tokens) per call. At an 8,192 window this has
  no planner effect. At 4,096 the per-call text budget drops ~10 %.
- **Still open, as a future `v3`:** the redundancy reduction / ROLE-RULES
  structure above. It should be judged by `prompt_eval_count` and field
  quality, not by characters.

## Planner / chunking follow-up

The KONE case shows that a large document can contain >13k relevant
characters even after normalization.

The next planner experiment should distinguish:

- **must-have significant passages**: provider/subject/number, term, notice,
  renewal, finance;
- **nice-to-have context**: neighbouring paragraphs, generic scope prose,
  document tail/head beyond what is necessary.

A preferred one-call strategy should be considered when all must-have
passages fit the **actual calculated call budget**, even if they exceed the
configured soft `target_chars`. Remaining budget can then be filled with
nice-to-have context.

Only use `chunked` when significant content truly does not fit the real
call budget.

If chunking remains necessary, investigate group-specific response schemas:

- `basis`;
- `term`;
- `finance`.

Today every chunk asks for the full contract response schema and the merge
later discards fields outside that chunk's group. A smaller per-group output
could reduce generation work on CPU, but it must be validated carefully
against current merge/conflict guarantees.

### Real staging baseline before Planner V2 (prompt v2, `heavy`, 8,192)

Measured on the HubDix staging host with `qwen2.5:1.5b-instruct`. These are
the regression baseline for Planner V2:

| contract | input | normalized | optimized | strategy | ai_calls | prompt_eval_count | eval_count | ollama_duration_ms | repair / fallback |
|---|---|---|---|---|---|---|---|---|---|
| ~17k | 17,168 | 16,482 | 6,952 | relevance | 1 | 3,853 (both runs) | 284 cold / 213 warm | ~68,097 cold / ~13,165 warm | no / no |
| ~78k KONE | 78,686 | 76,880 | 13,361 | chunked | 3 | 9,050 (both runs) | 1,153 / 1,331 | ~119,186 / ~75,080 | no / no |

Findings:

- **Tokens and latency must be read separately.** `prompt_eval_count` was
  identical in cold and warm runs. The latency gap is model load and cache
  state, not input size. Planner changes are therefore judged by
  `prompt_eval_count_total`, `ai_calls` and field quality, not by duration
  alone.
- **KONE quality issue:** the subject "KONE Care Servicevertrag" was
  plausible, but the same title also came back as `contract_number`.
  - The prompt rule exists ("only if explicitly printed in the document
    … never construct one"), and the merge only takes `contract_number`
    from the `basis` chunk. So the wrong value came from the `basis` call
    itself, i.e. model behavior with 1.5B parameters, not a lost passage.
  - Planner V2 can only reduce the risk indirectly: one call instead of
    three, and less filler. Tests cover the generic case (a title without
    a number keeps `null`; a non-`basis` chunk guessing the title is
    ignored).
  - If it persists after Planner V2, it is the first candidate for a
    prompt v3 rule (a document title/product name is not a contract
    number). No vendor-specific rule.

### Status (2026-11-04): Planner V2 implemented

Semantics are in
[contract-text-optimization.md, "Planner V2"](contract-text-optimization.md#planner-v2---mandatory-vs-supporting-2026-11-04).

- **Mandatory (within the hard per-call budget):** head + significant
  blocks with small neighbors and mid-sentence continuations, plus each
  group's best weak-only block.
- **Supporting:** tail (safety net), then weak hits and large neighbors,
  only within `target_chars` and at most `supporting_context_chars`
  (1,500) per call.
- **Chunking:** only when the mandatory blocks don't fit the hard budget.
  `target_chars` no longer decides it. The `target_chars: 5,500` paradox
  is covered by a test.
- **Synthetic, 8,192 window:**
  - 17k-type samples: ~6.4-6.9k → ~3.0-3.7k chars, still 1 call, all
    clauses kept;
  - a 25-clause-pair sample: chunked (3 calls, renewal + notice lost) → 1
    call with everything.
- **Expected on staging (to be confirmed, step 11 of the staging
  validation):**
  - ~17k: still 1 call, a clearly lower `prompt_eval_count`;
  - ~78k KONE: 1 call (2 at most), if its mandatory blocks fit the
    ~14,800-char budget. If KONE still needs 3 calls, the significant
    blocks really exceed one call. Then the group-specific schemas above
    are the next lever.

Planner V2 measurements (fill in from staging):

| contract | optimized | strategy | ai_calls | prompt_eval_count_total | eval_count_total | fields vs. baseline |
|---|---|---|---|---|---|---|
| short | | | | | | |
| ~17k | | | | | | |
| ~78k KONE | 40,629 | chunked | 3 | 17,083 (1 successful run) | 797 | - (see regression below) |

### Planner V2 regression on the real KONE contract (staging, 2026-11-05)

Same host, model, profile (`heavy`, 8,192), prompt v2 as the baseline.
Measured history, kept for comparison:

| planner | input | normalized | optimized | strategy | ai_calls | prompt_eval_count | eval_count | duration | result |
|---|---|---|---|---|---|---|---|---|---|
| before Planner V2 | 78,686 | 76,880 | 13,361 | chunked | 3 | 9,050 | 1,153 / 1,331 | ~119 s / ~75 s | ok |
| Planner V2, run 1 | 78,686 | 76,880 | 40,629 | chunked | 3 | unknown | - | ~180 s | `ai_timeout` |
| Planner V2, run 2 | 78,686 | 76,880 | 40,629 | chunked | 3 | unknown | - | ~180 s | `ai_timeout` |
| Planner V2, run 3 | 78,686 | 76,880 | 40,629 | chunked | 3 | 17,083 | 797 | 126.7 s | ok |

**Root cause**, reproduced synthetically with the `build_service_contract`
sample: V2 ends up chunked, 3 calls, 43,873 chars.

1. V2 made **every** strong hit mandatory. In a long service contract the
   strong patterns `Leistungsumfang`, `Preis`, `Vergütung`, `Kündigung`,
   `Laufzeit` and amounts recur in nearly every per-installation
   paragraph. In the synthetic sample 66 of 71 blocks were "significant",
   so the mandatory set could not fit one call.
2. In the chunked path every chunk planned its group's hits against the
   full hard budget (~14,800) instead of V1's `chunk_target_chars`
   (4,500). Each of the 3 chunks filled up to ~14k: 3 × ~14k ≈ 40k chars
   and ~17k prompt tokens, and the CPU box then ran into the 180 s limit.

`supporting_context_chars` played no part (supporting was ~0).

### Status (2026-11-05): Planner V2.1 implemented

Mandatory = **coverage**, not all hits. Per group and strong pattern:

- the best block (the clause whose heading names the topic, else the
  highest score);
- plus one block with a differing concrete value, as the contradiction
  guard;
- redundant repeats become supporting.

Details are in
[contract-text-optimization.md, "Planner V2.1"](contract-text-optimization.md#planner-v21---mandatory-coverage-bounding-2026-11-05).
No new config option, no prompt/schema/merge/normalization change.

Synthetic: the service-contract samples (42k / 70k) go from V2's 3 calls
with 43,873 chars (payment clause lost) to **1 call with ~3,700 chars** and
every core clause. The ~17k samples stay at 1 call, ~3.0-3.3k.

Planner V2.1 staging measurements (to be filled in, contract-text-optimization.md
"Staging validation" step 12):

| contract | input | normalized | mandatory | supporting | optimized | strategy | ai_calls | prompt_eval_count | eval_count | duration | result quality |
|---|---|---|---|---|---|---|---|---|---|---|---|
| short | | | | | | | | | | | |
| ~17k | 17,168 | 16,482 | | | | | | | | | |
| ~78k KONE | 78,686 | 76,880 | | | | | | | | | |

KONE gate:
- **required:** optimized far below 40,629, prompt_eval_count below
  17,083, no timeout;
- **preferred:** ≤ ~9,050 prompt tokens;
- **always:** no field worse than the pre-V2 baseline.

If KONE still needs 3 calls after V2.1, its unique evidence really
exceeds one call. Record which groups cause it; then group-specific
schemas are the next lever.

## Better timing visibility

`OllamaClient` already reads:

- `load_duration`;
- `prompt_eval_duration`;
- `eval_duration`;
- `eval_count`.

The next logging pass should surface these values in metadata-only AI logs,
preferably also per chunk, without ever logging prompts, document text or
model output.

This allows the next decision to be evidence-based:

- high prompt-eval time -> reduce prompt/input;
- high eval time -> reduce output/schema work;
- both high -> remaining bottleneck is mainly model/hardware.

### Status (2026-11-04): token counts implemented

- `OllamaClient` now also reads `prompt_eval_count`, i.e. the prompt tokens
  Ollama actually evaluated. Prefixes reused from Ollama's prompt cache are
  not counted.
- The contract diagnostics carry `prompt_eval_count_total` /
  `eval_count_total` (exact or `null`) and a per-call `calls[]` list. The
  analyze log line carries `prompt_eval_count` / `eval_count`.
- Semantics and the null rules are in
  [contract-text-optimization.md, "Ollama token telemetry"](contract-text-optimization.md#ollama-token-telemetry).
- No extra call and no local tokenizer.

Not yet measured: no Ollama was reachable in the development environment,
so the real numbers for the regression set (~17k, ~78k, short contract)
need a staging run (contract-text-optimization.md "Staging validation",
step 10). Fill in this table there:

| contract | raw_chars | normalized_chars | optimized_chars | ai_calls | prompt_eval_count | eval_count |
|---|---|---|---|---|---|---|
| ~17k | | | | | | |
| ~78k (KONE) | | | | | | |
| short (direct) | | | | | | |

These numbers answer the question for Planner V2: how many real prompt
tokens does `optimized_chars` become, and does a character reduction
translate into a token reduction? The phase durations
(`prompt_eval_ms`/`eval_ms`/`load_ms`) are read per call but not yet in
the log line.

## Assistant caller guidance

DocPipe deliberately does not know how to fetch HubDix data. The caller must
therefore minimize context before it reaches this API.

Real staging results show that smaller context/history materially reduce
latency. Consumers should prefer:

- summary KPIs instead of full row dumps for summary questions;
- one focused entity for entity questions;
- small sorted lists for "which items" questions;
- only the active domains needed for the question;
- only history turns necessary to resolve a follow-up reference.

The API guards (100k context, 8 history turns) are **safety maxima, not
recommended operating sizes**.

Deterministically answerable facts should ideally be handled by the caller
without an LLM call at all.

## Background processing boundary

A consumer such as HubDix may choose to analyze imported contracts in a
cron/background workflow so a manager sees prepared suggestions later.

That orchestration remains **outside DocPipe**:

```text
consumer import/queue
   -> DocPipe extract/analyze
   -> consumer stores review suggestion
```

DocPipe remains stateless and does not gain a database, persistent queue or
background scheduler.

This distinction is intentional and should remain explicit in future work.

## Regression set for the next pass

At minimum compare every normalization/prompt/planner change against:

1. a ~17k-character real contract;
2. the ~78k-character KONE contract;
3. at least one short contract that stays `strategy=direct`;
4. assistant hotel-health and cross-domain questions with measured
   context/history sizes.

For contracts compare both latency and extracted business fields. A faster
result that loses notice, renewal, provider or price information is a
regression.

## Out of scope for this optimization pass

- RAG / embeddings / vector database;
- AI-based OCR cleanup;
- AI summarization before extraction;
- persistent DocPipe jobs/queue;
- automatic business-field writes;
- parallel Ollama calls as the default optimization;
- model replacement before the text/prompt measurements above are complete.
