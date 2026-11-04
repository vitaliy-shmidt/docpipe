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

1. transient text normalization V2;
2. compact `contract_extraction` prompt V2;
3. expose the already-read Ollama phase metrics in safe logs;
4. improve relevance/chunking decisions against the real prompt/context
   budget;
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
