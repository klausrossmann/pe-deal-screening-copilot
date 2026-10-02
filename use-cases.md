# Use cases

How the four use cases from [project-plan.md](project-plan.md) are implemented, in the order they were built:
asking a single question, generating a full screening report for one company, comparing two companies, and
screening the whole portfolio against one or more criteria. All four share the same retrieval stage — see
[architecture.md](architecture.md) for the overall data flow and [retrieval.md](retrieval.md) for how a question
becomes ranked evidence chunks — and build on each other: UC2 loops UC1's graph shape once per criterion, UC3 and
UC4 both reuse UC2's per-company/per-criterion functions rather than introducing new retrieval or prompting logic.

## How the four use cases fit together

```
                     UC1 Ask                     UC2 Screening (one company, all criteria)
                        │                                   │
                        │                     screen_criterion(criterion, company)  ◄── the one building block
                        │                                   │                        reused by UC2, UC3, UC4
                        ▼                                   ▼
              retrieve() one query          1. saved result still valid?  ── yes ──► return it (no LLM call)
                        │                   2. retrieve_multi(): question + retrieval_queries, distance cutoff
                        ▼                   3. nothing found?  ── yes ──► insufficient_evidence (no LLM call)
                  LLM answer                4. LLM classifies the evidence (risk-aware prompt)
                                            5. save the result to data/analysis/

   UC3 Compare   = UC2 for two companies at once, merged by criterion
   UC4 Universe  = screen_criterion() for every (company, criterion) pair you pick
```

Three things make this cheap and consistent across use cases:

1. **Saved results.** Steps 1 and 5: every (company, criterion) assessment is saved as JSON and reused by every
   use case until something that could change it changes. Screening ATOSS in UC2 makes ATOSS free in UC3 and UC4.
2. **Better evidence, fewer pointless LLM calls.** Step 2 searches several phrasings per criterion and drops
   chunks above a distance cutoff, so step 3 can genuinely skip the LLM when the documents don't cover a criterion.
3. **Everything runs concurrently.** UC2–UC4 start all their `screen_criterion()` calls at once. The shared
   `ChatService` decides how many LLM calls are actually in flight (`LLM_MAX_CONCURRENCY`), so free-tier rate
   limits are respected in one place instead of in every workflow.

## UC1: Ask

How a question turns into an answer with citations. This is UC1 from `project-plan.md`: basic RAG question
answering, one company at a time.

### What it does

```
question
    │
    ▼
retrieve(question, company_id, top_k, max_distance)   -- same retrieval stage as before
    │
    ▼
no chunks found (or none close enough)? -> "Insufficient evidence" answer, LLM is never called
    │
    ▼
build a numbered-sources prompt from the chunks
    │
    ▼
LLM generates an answer, citing sources inline as [1], [2], ...
    │
    ▼
{ question, company_id, answer, sources: [{document, file_name, page, distance}, ...] }
```

Implemented as a two-node [LangGraph](https://langchain-ai.github.io/langgraph/) graph in `app/workflows/ask.py`:
`retrieve` → `generate`. LangGraph is used here, not in ingestion or retrieval, because it's a separate
orchestration layer for the multi-step `ask` / `screen` / `compare` workflows below — plain function calls are
enough for the single-query retrieval stage, but screening (UC2) reuses this same graph shape once per criterion,
which is where a graph abstraction starts paying for itself.

### Design decisions

- **Two nodes, explicit state.** `AskState` is a `TypedDict` with `question`, `company_id`, `top_k`, `chunks`,
  `answer`. Each node returns only the keys it updates; LangGraph merges them into the running state. This keeps
  the graph trivially testable — a node is just a function of state in, partial state out.
- **Services are injected, not constructed inside the graph.** `build_ask_graph(embedding_service, chat_service)`
  takes both as arguments, mirroring how `retrieve()` takes an `embedding_service` instead of creating its own.
  Tests pass in fakes for both; the API route passes in real ones.
- **One shared service instance per process.** The API gets its services from `get_embedding_service()` /
  `get_chat_service()` (FastAPI `Depends`, cached with `lru_cache`). Before, every request built a new
  `EmbeddingService()`, which reloaded the local embedding model from disk each time.
- **No LLM call when there's no evidence.** If `retrieve` returns zero chunks, `generate_node` short-circuits to
  a fixed `"Insufficient evidence..."` answer instead of prompting the model. This is the same principle
  `framework.md` describes for screening: saying "I don't know based on the documents I have" is a correct
  answer, not a failure. With `RETRIEVAL_MAX_DISTANCE` set, this also happens for questions the documents simply
  don't cover, not only for companies without documents (see [retrieval.md](retrieval.md)).
- **Citations are the retrieved chunks, not something parsed out of the LLM's text.** The prompt asks the model
  to cite `[1]`, `[2]`, etc. inline, but the `sources` returned to the caller are always exactly the chunks that
  were retrieved and fed into the prompt — there's no fragile parsing of the model's output to recover which
  chunk backs which sentence. If a citation number doesn't make sense, the full source list is still there to
  check against.
- **Raw provider SDKs, not a LangChain chat-model wrapper.** `app/services/llm.py` mirrors
  `app/services/embeddings.py`: a provider switch (`LLM_PROVIDER` — `openai`, `google`, or `groq`) over the plain
  SDKs, imported lazily. LangGraph only needs plain Python functions for its nodes, so there was no reason to add
  `langchain-openai` / `langchain-google-genai` just for one `generate(prompt) -> str` call. Note this means the
  LLM service and the embedding service currently use two different Google SDKs: `app/services/llm.py` uses the
  current `google-genai` (`from google import genai`), while `app/services/embeddings.py` still uses the
  deprecated `google-generativeai` — only worth unifying if/when `EmbeddingService` is touched for another reason.
- **Google calls retry transient failures with backoff.** The free-tier Flash models occasionally return
  `503 UNAVAILABLE` ("This model is currently experiencing high demand... usually temporary") under load. The
  `genai.Client` is constructed with `http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(...))`
  (5 attempts, exponential backoff from 1s up to 20s) so a transient 503 is retried instead of failing the whole
  `ask()` call. This only helps with genuinely transient errors — a `404` (wrong model name) or a `429` with a
  `0` free-tier quota (wrong pricing tier) will still fail after retrying, since those aren't temporary.
- **Rate limits are handled in `ChatService`, not in the workflows.** The provider SDKs are synchronous, so each
  call runs in a worker thread (`asyncio.to_thread`) and the event loop stays free. A semaphore caps how many
  calls are in flight (`LLM_MAX_CONCURRENCY`, default `1`), and the OpenAI/Groq clients retry up to 8 times,
  waiting for the server's `retry-after` each time. Groq's free tier allows only 8,000 tokens per minute (about 8
  screening calls), so with 4 calls in flight a full report failed with `429` even with retries. With 1 it
  completes reliably. On a paid tier, raise `LLM_MAX_CONCURRENCY` and screening gets proportionally faster.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.ask import ask

result = await ask(
    "What evidence is there of recurring revenue at ATOSS?",
    EmbeddingService(),
    ChatService(),
    company_id="atoss",
    top_k=5,
)
# {"question": ..., "company_id": "atoss", "answer": "...[1]...", "sources": [...]}
```

Through the API:

```bash
curl -X POST http://localhost:8000/ask \
  -H "content-type: application/json" \
  -d '{"question": "What evidence is there of recurring revenue?", "company_id": "atoss", "top_k": 5}'
```

### Configuration

Same pattern as `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL`:

| Variable | Values | Default |
| --- | --- | --- |
| `LLM_PROVIDER` | `openai`, `google`, `groq` | `openai` |
| `LLM_MODEL` | any chat model name for that provider | `gpt-4o-mini` (openai) / `models/gemini-flash-latest` (google) / `openai/gpt-oss-120b` (groq) |
| `OPENAI_API_KEY` / `GOOGLE_API_KEY` / `GROQ_API_KEY` | required for the selected provider | — |
| `LLM_MAX_CONCURRENCY` | max LLM calls in flight at once, shared by all requests | `1` |
| `RETRIEVAL_MAX_DISTANCE` | cosine-distance cutoff for retrieved chunks (see [retrieval.md](retrieval.md)) | unset = no cutoff; `.env.example` uses `0.70` |

A missing or invalid API key raises immediately on `ChatService()` construction, the same way `EmbeddingService()`
does for `openai` / `google`.

**Groq** uses the official `groq` SDK, whose client is already OpenAI-compatible (`client.chat.completions.create`),
so it shares the same branch as `openai` in `ChatService.generate()`. Groq's free tier covers open-weight models
(the default here is `openai/gpt-oss-120b`) with generous rate limits and fast inference — currently the default
`LLM_PROVIDER` for this project, since it avoids both Google's shifting model availability (see below) and
OpenAI's paid-only API.

**Google model availability changes often — don't assume a model name is free or even callable.** As of this
writing, `models/gemini-2.5-pro` and `models/gemini-2.5-flash` both 404 for new API keys ("no longer available to
new users"), and newer Pro-tier models (e.g. `gemini-3.1-pro`) return `429 ResourceExhausted` with a `0` free-tier
quota — only Flash-tier models have a usable free quota. `models/gemini-flash-latest` is a rolling alias Google
keeps pointed at a current, free-tier-eligible Flash model, which is why it's the default here instead of a
pinned version number. If `ChatService` starts failing with `404 NotFound` or `429 ResourceExhausted`, check
`client.models.list()` for what your key can actually call before assuming the code is broken.

### Running it

Assumes the shared setup from [architecture.md](architecture.md), and that [ingestion.md](ingestion.md) has
already populated the database. Also needs `LLM_PROVIDER`/`OPENAI_API_KEY` (or `GOOGLE_API_KEY`) set in `.env` —
there's no local, API-key-free option for generation the way `sentence_transformers` is for embeddings.

```bash
python3 - <<'PY'
import asyncio
from app.retrieval.retriever import retrieve  # noqa: F401 (same corpus as retrieval.md)
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.ask import ask

async def main():
    result = await ask(
        "What evidence is there of recurring revenue?",
        EmbeddingService(),
        ChatService(),
        company_id="atoss",
        top_k=5,
    )
    print(result["answer"])
    for s in result["sources"]:
        print("-", s["document"], "p.", s["page"])

asyncio.run(main())
PY
```

**Tests:**

```bash
python3 -m pytest tests/test_ask.py -q
```

`AskNoEvidenceTestCase` patches `retrieve` to return no chunks and checks the LLM is never called — no Postgres,
no API key needed. `AskWorkflowTestCase` inserts a hand-picked chunk for a fake `demo` company (same pattern as
`RetrieveTestCase` in `retrieval.md`) and checks the graph grounds a fake LLM's answer in it — needs Postgres, not
an API key, since the chat service is a dummy.

### Deliberately not built yet

- conversation memory / multi-turn follow-up questions — `ask()` is stateless, one question in, one answer out
- streaming responses — the LLM call is a single blocking `generate()`, not a token stream

## UC2: Screening

How a company turns into a full PE screening report: one assessment per criterion in the screening framework,
each grounded in retrieved evidence. This is UC2 from `project-plan.md`, Phase 5 — the hero feature of the
project.

### What it does

```
company_id
    │
    ▼
load config/screening_config.yaml -> list of criteria (each with dimension, polarity, retrieval_queries)
    │
    ▼
for every criterion, all started at once (screen_criterion):
    │
    ├─ step 1: a saved result in data/analysis/<company>/<criterion>.json still matches?
    │          -> return it with "cached": true, nothing else runs
    │
    ├─ step 2: retrieve_multi([question, *retrieval_queries], company_id, top_k, max_distance)
    │          -> one search per phrasing, merged, each chunk once at its best distance
    │
    ├─ step 3: no chunks found? -> assessment = insufficient_evidence, LLM is never called
    │
    ├─ step 4: numbered-sources prompt (plus a "this is a RISK" note for risk criteria);
    │          LLM returns {"assessment": ..., "rationale": "...[1]..."} as JSON, parsed tolerantly;
    │          anything that doesn't parse into a known level becomes insufficient_evidence
    │
    └─ step 5: save the result (with a fingerprint of everything that produced it)
    │
    ▼
group the per-criterion results by dimension (in config order)
    │
    ▼
{ company_id, dimensions: [{ dimension, description, polarity, criteria: [{criterion, dimension, polarity,
  question, assessment, rationale, sources, cached}, ...] }, ...] }
```

Implemented in `app/workflows/screening.py`:

- `load_screening_config()` reads `config/screening_config.yaml` into a flat list of `Criterion` (id, question,
  dimension, dimension description, polarity, retrieval queries). `Criterion.queries` is what gets searched: the
  question first, then the extra phrasings.
- `build_screen_criterion_graph()` is a two-node [LangGraph](https://langchain-ai.github.io/langgraph/) graph —
  `retrieve` → `assess` — the same shape as UC1's `retrieve` → `generate`, with `assess` producing a classified
  assessment instead of free text. It is compiled once per pair of services and reused for every call.
- `screen_criterion()` runs steps 1–5 above for one criterion and one company.
- `screen_company()` loads the config and runs `screen_criterion()` for every criterion concurrently, grouping
  the results by dimension. An optional `dimension` parameter filters the config down to one dimension first —
  added for UC3 (Compare) below.

Saving results lives in `app/workflows/analysis_cache.py` (`AnalysisCache`), described under *Saved results*
below.

### Design decisions

- **Reuses the UC1 graph shape, not the UC1 graph itself.** Screening loops a `retrieve` → LLM graph once per
  criterion. The LLM step is different enough (structured classification vs. free-text answer with citations)
  that it's a separate `assess` node and prompt, not a parameterized reuse of `generate_node`.
  `format_sources_block()` and `to_source_citations()` — the two bits that *are* identical — were pulled out
  into `app/workflows/common.py` so `ask.py` and `screening.py` share them instead of duplicating the formatting
  logic.
- **Evidence classification, not a score.** `framework.md` is explicit about this: the LLM is asked to classify
  evidence into `strong_evidence` / `moderate_evidence` / `weak_evidence` / `insufficient_evidence`, never to
  invent a numeric score. The prompt enforces this with an exact enum of allowed values.
- **No LLM call when there's no evidence**, same principle as UC1: if `retrieve` returns zero chunks for a
  criterion, `assess_node` short-circuits straight to `insufficient_evidence` instead of prompting the model to
  guess.
- **Structured output via prompted JSON, not a provider-specific schema API.** `ChatService.generate()` is a
  single `prompt -> str` call shared across `openai`/`google`/`groq` (see UC1's Configuration above), so there's
  no common structured-output API to hook into without provider-specific branching. Instead, the prompt asks for
  a JSON object with exactly two keys, and `_parse_assessment()` extracts the first `{...}` block with a regex
  before calling `json.loads` — tolerant of a model wrapping the JSON in a code fence or a sentence of preamble.
- **Unparseable or out-of-enum output still produces a usable result.** If the LLM's reply isn't valid JSON, or
  its `assessment` value isn't one of the four allowed levels, `_parse_assessment()` falls back to
  `insufficient_evidence` with an explanatory rationale rather than raising. A broken LLM response degrades to
  "we don't have conclusive evidence," not a 500 error for the whole report.
- **Concurrent, with the limit in one place.** `screen_company()` starts all ~20 `screen_criterion()` calls at
  once with `asyncio.gather`. Retrieval and embedding run in worker threads, so the searches genuinely overlap.
  The LLM calls queue behind `ChatService`'s semaphore (`LLM_MAX_CONCURRENCY`, see UC1's design notes). The
  workflows don't need to know about rate limits: on Groq's free tier the LLM step stays effectively sequential
  (a fresh ATOSS report took ~110 s); on a paid tier, raising the limit speeds everything up with no code change.
- **One criterion failing doesn't take down the report.** `ChatService`'s retries (UC1's Configuration above)
  only cover *transient* failures. A sustained one — e.g. Groq's free tier also has a **daily** token budget
  (200,000/day), not just per-minute, and the 5+ minute wait that implies outlasts the retries — would otherwise
  make one failing `asyncio.gather` task raise and fail the whole request, discarding every criterion that had
  already succeeded. `screen_company()` and `screen_universe()` therefore call `screen_criterion_safe()`, not
  `screen_criterion()` directly: it catches any exception and returns `insufficient_evidence` with `"error":
  true` and the exception message as the rationale, instead of raising. A failed criterion is not saved to the
  cache (it isn't a real assessment), so it's retried on the next request. Every criterion result — cached,
  freshly assessed, or failed — carries `"error": false`/`true`, so a consumer never has to guess which kind of
  `insufficient_evidence` it's looking at.
- **Grouped by dimension in the response, not a flat list.** The report shape in `project-plan.md`'s UC2 mockup
  is organized by dimension (Business Quality, Growth, ...), so `screen_company()` returns
  `dimensions: [{dimension, description, polarity, criteria: [...]}]` instead of one flat array the caller would
  have to group itself.

### Risk criteria (polarity)

The framework shows risks differently from strengths (`! Evidence found` rather than `✓ Strong`). Using the same
scale for both would read *"strong evidence of customer concentration"* as something good. So:

1. **Config:** the `risks` dimension sets `polarity: risk`. Every other dimension defaults to `positive`.
2. **Prompt:** for risk criteria the LLM gets an extra paragraph saying that `strong_evidence` means *the risk is
   clearly present* (a red flag), not that the company is strong.
3. **Response:** every criterion and dimension carries `polarity`, so consumers never have to guess.
4. **UI:** risk criteria get their own labels (🔴 *Risk clearly present* / 🟠 *Risk indicated* / 🟡 *Weak risk
   signal*) instead of the green/yellow scale.

The evidence levels themselves stay the same four values, so filtering by `min_assessment` (UC4) works the same
way for both kinds. Absence of evidence is still `insufficient_evidence`, never "no risk". The documents not
mentioning a risk is not proof that the risk is absent.

### Retrieval queries

Each criterion can list `retrieval_queries`: extra search phrases worded like the documents (*"EBITDA margin"*,
*"free cash flow"*) rather than like a question. Step 2 searches the question **and** every phrase, then merges
the results. The question alone is still what the LLM is asked. Measured across the 8 ingested companies, this
moved the best match much closer for several criteria (average top-1 distance: `organic_growth` 0.65 → 0.43,
`margin` 0.49 → 0.33, `geographic_concentration` 0.66 → 0.47; details in [retrieval.md](retrieval.md)).

### Saved results

`framework.md` (section 6) suggests caching generated analyses under `data/analysis/`. That's what
`AnalysisCache` does:

```
data/analysis/
├── atoss/
│   ├── recurring_revenue.json
│   └── ...
└── nemetschek/
    └── ...
```

Each file holds the result (assessment, rationale, sources with `chunk_id`), when it was assessed, and a
**fingerprint**: a hash of everything that could change the answer:

| Part of the fingerprint | Changes when you... |
| --- | --- |
| question + `retrieval_queries` + polarity | edit the criterion in `screening_config.yaml` |
| prompt template | change the prompt in `screening.py` |
| `top_k`, `max_distance` | change retrieval settings |
| embedding provider/model, LLM provider/model | switch models |
| corpus fingerprint (hash of the company's documents) | ingest, re-ingest or remove a document for that company |

On the next request, `screen_criterion()` recomputes the fingerprint (one small DB query). If it matches the
saved one, the saved result is returned with `"cached": true`; otherwise the criterion is re-assessed and the
file overwritten. There is no manual invalidation step to forget. In the real-corpus check, a second ATOSS report
took 0.14 s instead of ~110 s, and a later UC4 run reused the saved ATOSS rows.

Notes:

- `refresh: true` (API) / the *Recompute* checkbox (UI) ignores saved results for that request and overwrites
  them, e.g. to see how much the LLM's answer varies between runs.
- Saved results also make demos stable: the same report doesn't change every time it's opened.
- Files are written to a temp file and then renamed, so a crash never leaves half-written JSON behind.
- `company_id` comes from the request and becomes part of a file path, so `AnalysisCache` only accepts ids made of
  letters, digits, `_` and `-` (anything else, e.g. `../etc`, is rejected with HTTP 400).
- The workflow functions only save results when a `cache` is passed in. The API always passes one; tests and
  scripts don't unless they want to.
- `data/analysis/` is git-ignored. Deleting it is always safe; it just costs LLM calls to rebuild.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company

result = await screen_company("atoss", EmbeddingService(), ChatService(), top_k=3)
# {"company_id": "atoss", "dimensions": [
#   {"dimension": "business_quality", "description": "...", "polarity": "positive", "criteria": [
#     {"criterion": "recurring_revenue", "dimension": "business_quality", "polarity": "positive",
#      "question": "...", "assessment": "strong_evidence", "rationale": "...[1]...",
#      "sources": [{"chunk_id": "...", "document": "...", "file_name": "...", "page": 3, "distance": 0.24}],
#      "cached": false},
#     ...
#   ]},
#   ...
# ]}
```

Called like this, nothing is saved and there is no distance cutoff. To get the same behaviour as the API, pass
both explicitly:

```python
from app.retrieval.retriever import max_distance_from_env
from app.workflows.analysis_cache import AnalysisCache

result = await screen_company(
    "atoss", EmbeddingService(), ChatService(), top_k=3,
    max_distance=max_distance_from_env(), cache=AnalysisCache("data/analysis"),
)
```

A single criterion can be assessed directly with `screen_criterion(criterion, company_id, embedding_service,
chat_service, top_k, max_distance=None, cache=None, refresh=False)`, where `criterion` is one `Criterion` from
`load_screening_config()`.

Through the API:

```bash
curl -X POST http://localhost:8000/screen \
  -H "content-type: application/json" \
  -d '{"company_id": "atoss", "top_k": 3}'            # add "refresh": true to ignore saved results
```

### Configuration

`config/screening_config.yaml` defines the framework: a `screening_dimensions` mapping, each dimension with a
`description`, an optional `polarity` (`positive` by default, or `risk`), and a `criteria` mapping of
`criterion_id -> {question, retrieval_queries}`. Adding a criterion or a whole dimension is a config change only
— no code change needed, since `load_screening_config()` reads the structure generically. An unknown `polarity`
value raises `ValueError` when the config is loaded.

```yaml
  risks:
    description: Material risks to the investment thesis
    polarity: risk
    criteria:
      customer_concentration:
        question: "Is there evidence of customer concentration?"
        retrieval_queries:
          - "largest customers' share of revenue"
          - "dependence on a few major customers"
```

Writing good `retrieval_queries`: phrase them the way an annual report or investor deck would state the fact, not
as a question, and keep each one short and about a single idea. Check the effect with the snippet in
[retrieval.md](retrieval.md) (smaller distance = closer match).

Same LLM/embedding configuration as UC1 above (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_MAX_CONCURRENCY`,
`RETRIEVAL_MAX_DISTANCE`, provider API keys), plus `ANALYSIS_DIR` (default `data/analysis`) for where results
are saved.

### Running it

Assumes the shared setup from [architecture.md](architecture.md), with the database already populated
([ingestion.md](ingestion.md)) and an `LLM_PROVIDER`/API key set, same as UC1 above.

```bash
python3 - <<'PY'
import asyncio
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company

async def main():
    result = await screen_company("atoss", EmbeddingService(), ChatService(), top_k=3)
    for dimension in result["dimensions"]:
        print("==", dimension["dimension"], "==")
        for c in dimension["criteria"]:
            print(f"  {c['criterion']}: {c['assessment']}")

asyncio.run(main())
PY
```

**Tests:**

```bash
python3 -m pytest tests/test_screening.py -q
```

- `LoadScreeningConfigTestCase` / `ParseAssessmentTestCase` — pure unit tests, no Postgres, no LLM: the config
  loader (including risk polarity, `retrieval_queries`, and rejecting an unknown polarity) and the JSON-parsing
  fallbacks (clean JSON, code-fenced JSON, invalid JSON, out-of-enum value).
- `ScreenCriterionNoEvidenceTestCase` — patches `retrieve_multi` to return no chunks and checks the LLM is never
  called, same pattern as `AskNoEvidenceTestCase` in `test_ask.py`.
- `ScreenCriterionQueriesAndPolarityTestCase` — pure unit test: checks the question plus every retrieval query is
  searched with the distance cutoff, and that only risk criteria get the "this is a RISK" prompt note.
- `ScreenCompanyWorkflowTestCase` — needs Postgres; inserts a hand-picked chunk for a fake `demo` company and
  checks a dummy chat service's fixed JSON reply is parsed and grouped by dimension correctly.
- `ScreenCompanyDimensionFilterTestCase` — pure unit test: an unknown `dimension` id raises, and a known
  dimension narrows the run to just that dimension's criteria (patches `screen_criterion`).
- `tests/test_analysis_cache.py` — pure unit tests in a temp directory: save/load round trip, a changed
  fingerprint means stale, unsafe ids (`../etc`) are rejected; and through `screen_criterion()`: the second run
  is served from the saved result without an LLM call, `refresh=True` forces a new one, and changed documents or
  settings invalidate it.

### Deliberately not built yet

- golden-question evaluation of screening output (Phase 7) — `test_screening.py` covers the workflow's code
  paths, not evidence/groundedness/citation quality against a verified set of questions. Saved results in
  `data/analysis/` are a natural input for that later.
- a client-side token-rate limiter — on free tiers, `LLM_MAX_CONCURRENCY=1` plus SDK retries is enough
- partial reports when one criterion's LLM call fails for good — the whole request still fails, as before; the
  criteria that did succeed are already saved, so a retry only redoes the missing ones

## UC3: Compare

How the same screening criteria get applied to two companies side by side, to answer questions like
"Nemetschek vs. ATOSS on recurring revenue" instead of reading two separate reports and comparing them by hand.
This is UC3 from `project-plan.md`.

### What it does

```
company_a_id, company_b_id, optional dimension (e.g. "growth")
    │
    ▼
screen_company(company_a_id, ..., dimension=dimension)  ┐ both started at once;
screen_company(company_b_id, ..., dimension=dimension)  ┘ saved results are reused for either company
    │
    ▼
walk company_a's dimensions/criteria; for each criterion, look up the matching
criterion (same id) in company_b's results
    │
    ▼
{ company_a, company_b, dimension, dimensions: [{ dimension, description, polarity, criteria: [
  { criterion, question, polarity, company_a: {assessment, rationale, sources, cached},
    company_b: {assessment, rationale, sources, cached} }, ... ] }, ...] }
```

Implemented in `app/workflows/compare.py`:

- `compare_companies()` is the only function here. It does not introduce a new LangGraph graph or a new
  retrieve/assess step — it runs `screen_company()` for both companies concurrently and merges the two
  per-company reports into one side-by-side structure, keyed by criterion id.
- `screen_company()` in `screening.py` (UC2 above) gained an optional `dimension` parameter for this: when set,
  it filters `load_screening_config()`'s criteria to that one dimension before looping, instead of assessing all
  ~20 criteria when the caller only wants e.g. "Growth". An unknown dimension id raises `KeyError`, listing the
  valid ids, the same pattern as `screen_universe()`'s unknown-criterion error in UC4 below.

### Design decisions

- **Reuses `screen_company()`, not `screen_criterion()` directly.** `project-plan.md`'s UC3 mockup is a
  dimension-filterable report for each company ("Business Quality / Growth / Profitability / Buy & Build /
  Risks / [All]"), which is exactly what `screen_company()` already produces — grouped by dimension, each
  criterion assessed independently. Comparison only needs to run that twice and zip the results together by
  criterion id; it doesn't need its own retrieval or prompting logic.
- **The `dimension` filter lives on `screen_company()`, not as post-filtering in `compare_companies()`.**
  Filtering after the fact would still retrieve and call the LLM for every criterion outside the requested
  dimension, wasting calls for no reason. Pushing the filter into `load_screening_config()`'s result inside
  `screen_company()` means an unwanted dimension's criteria are never assessed in the first place — for either
  company.
- **Two full company reports, not a single merged retrieval/prompt.** Each company's evidence lives in
  different chunks, so there's no shared retrieval step to merge the way UC1/UC2 share one query. The two
  `screen_company()` calls are independent, so they run concurrently. `ChatService` still caps the LLM calls in
  flight, same as UC2.
- **Comparing companies you've already screened is nearly free.** Both reports come from the saved results (see
  UC2, *Saved results*): if ATOSS and Nemetschek were screened in UC2 before, `/compare` makes no LLM calls at
  all. Each side carries `cached` so the UI can show which assessments are reused.
- **Risk criteria are labelled as risks.** Each merged criterion carries `polarity`, so *"Nemetschek: strong
  evidence"* on `competition` is shown as 🔴 *Risk clearly present*, not as a strength.
- **Grouped by dimension, flat merge by criterion id within each dimension.** Both companies are assessed
  against the identical `config/screening_config.yaml`, so every criterion id in company A's results has a
  matching entry in company B's — no fuzzy matching needed, just a dict keyed by `criterion.id` per dimension.
- **No "winner" is computed.** Same principle as `project-plan.md`'s UC3 description: "the point is consistent
  comparison against a common framework, not declaring a winner." The response gives both companies'
  `assessment`/`rationale`/`sources` per criterion; any side-by-side "who's stronger" judgment is left to the
  UI/consumer, not this function.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.compare import compare_companies

result = await compare_companies(
    "nemetschek", "atoss", EmbeddingService(), ChatService(), top_k=3, dimension="growth",  # dimension is optional
)
# {"company_a": "nemetschek", "company_b": "atoss", "dimension": "growth", "dimensions": [
#   {"dimension": "growth", "description": "...", "criteria": [
#     {"criterion": "organic_growth", "question": "...",
#      "company_a": {"assessment": "strong_evidence", "rationale": "...", "sources": [...]},
#      "company_b": {"assessment": "moderate_evidence", "rationale": "...", "sources": [...]}},
#     ...
#   ]},
# ]}
```

Omitting `dimension` (or passing `None`) compares across every dimension in `config/screening_config.yaml`,
matching UC3's `[All]` option.

Through the API:

```bash
curl -X POST http://localhost:8000/compare \
  -H "content-type: application/json" \
  -d '{"company_a": "nemetschek", "company_b": "atoss", "top_k": 3, "dimension": "growth"}'   # optional "refresh": true
```

An unknown `dimension` id or an invalid company id returns `400` with a message explaining why (via
`KeyError`/`ValueError`, caught in `app/api/screening.py`).

### Running it

Assumes the shared setup from [architecture.md](architecture.md), with the database already populated
([ingestion.md](ingestion.md)) for both companies being compared, and an `LLM_PROVIDER`/API key set, same as
UC2 above.

```bash
python3 - <<'PY'
import asyncio
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.compare import compare_companies

async def main():
    result = await compare_companies("nemetschek", "atoss", EmbeddingService(), ChatService(), top_k=3)
    for dimension in result["dimensions"]:
        print("==", dimension["dimension"], "==")
        for c in dimension["criteria"]:
            print(f"  {c['criterion']}: {c['company_a']['assessment']} vs {c['company_b']['assessment']}")

asyncio.run(main())
PY
```

**Tests:**

```bash
python3 -m pytest tests/test_compare.py -q
```

- `CompareCompaniesValidationTestCase` — pure unit test: an unknown `dimension` id raises, no Postgres or LLM
  involved.
- `CompareCompaniesMergeTestCase` — pure unit test: `screen_company` is patched to return two canned
  per-company reports, checking only the criterion-by-criterion merge logic in isolation.
- `CompareCompaniesWorkflowTestCase` — needs Postgres; inserts the same chunk for two fake companies (`demo`,
  `demo2`) and runs the real `screen_company()`/`screen_criterion()` retrieval against both with a dummy chat
  service, same pattern as `ScreenCompanyWorkflowTestCase` in UC2 above.

### Deliberately not built yet

- comparing more than two companies at once — `project-plan.md`'s UC3 mockup is explicitly two companies
  ("Company A vs. Company B"); comparing a larger set is closer to UC4's universe screening, which already
  exists for the single-criterion case
- combining multiple criteria into one dimension-level "who's ahead" verdict — same reasoning as UC2/UC4: left
  to the caller, not baked into the workflow

## UC4: Universe screening

How one or more screening criteria get evaluated across every ingested company at once, to answer questions like
"which companies have strong evidence of recurring revenue?" or "how do all companies look on the four Buy & Build
criteria?" instead of "tell me about ATOSS." This is UC4 from `project-plan.md` — the stretch feature, explicitly
"mostly reuse of UC2."

### What it does

```
criterion ids (e.g. ["acquisition_history", "acquisition_strategy"]), min_assessment (e.g. "moderate_evidence")
    │
    ▼
look up the criteria in config/screening_config.yaml -> unknown ids raise KeyError, an empty list ValueError
    │
    ▼
list_companies_with_documents() -> every company_id with at least one ingested document
    │
    ▼
for every (company, criterion) pair, all started at once:
    screen_criterion(criterion, company_id, ...)   -- same building block as UC2, saved results reused
    │
    ▼
mark each row meets_threshold = assessment at least as strong as min_assessment
    │
    ▼
{ criteria: [{criterion, dimension, polarity, question}, ...], min_assessment,
  results: [{company_id, criterion, dimension, polarity, question, assessment, rationale, sources,
             cached, meets_threshold}, ...],          -- one row per (company, criterion)
  matches: [...the rows with meets_threshold = true...] }
```

Implemented in `app/workflows/universe.py`:

- `screen_universe()` is the only function here. It does not introduce a new LangGraph graph — it runs
  `screen_criterion()` from `screening.py` for every (company, criterion) pair, the same way `screen_company()`
  runs it for every criterion of one company.
- `list_companies_with_documents()` (in `app/db/repository.py`) returns the distinct `company_id`s that have at
  least one row in `documents`, which is the actual screenable universe — not every id in `companies.yaml`,
  some of which may not have been ingested yet.

### Design decisions

- **Criterion ids, not a free-text query.** `project-plan.md`'s UC4 examples ("find companies with
  strong evidence of recurring revenue") map directly onto criterion ids from `screening_config.yaml`. Rather
  than adding an NLP layer to match free text to the nearest criterion, the caller passes the ids directly —
  they're already a stable, known vocabulary shared with UC2. A single id as a plain string is also accepted.
- **Several criteria per call, but no combined verdict.** `framework.md`'s "buy & build" example looks at four
  criteria together. `screen_universe()` accepts any list of criteria and returns one row per (company,
  criterion), which the UI shows as a company × criterion table. It deliberately does **not** combine those rows
  into one judgment per company (e.g. "all four must be at least moderate") or rank companies: that would be
  guessing at an investment judgment that belongs to the analyst, which `framework.md` explicitly warns against
  ("not making an investment decision").
- **The universe is "ingested companies," not "configured companies."** `companies.yaml` can list companies
  before their documents are ingested (that's the point of `data.md`'s staged rollout). Screening a company with
  no chunks would just produce `insufficient_evidence` for everything, so `list_companies_with_documents()`
  queries `documents` directly instead of reading `companies.yaml`, keeping the universe accurate as ingestion
  progresses.
- **`min_assessment`, not a fixed filter list.** `ASSESSMENT_LEVELS` in `screening.py` is already ordered
  strongest-first (`strong_evidence` > `moderate_evidence` > `weak_evidence` > `insufficient_evidence`). A
  `min_assessment` threshold (e.g. `"moderate_evidence"` matches both `strong_evidence` and `moderate_evidence`)
  is a simpler caller-facing concept than passing an explicit set of acceptable levels, and reuses that same
  ordering instead of introducing a second enum.
- **For risk criteria, a match means "risk flagged".** The threshold always means *evidence at least this
  strong*. For a risk criterion like `customer_concentration`, that's evidence that the risk **is present**, so
  the response carries `polarity` and the UI warns that ✅ on a risk column means *flagged*. It is deliberately
  not inverted into "companies without the risk": `insufficient_evidence` means the documents don't say, not
  that the risk is absent.
- **Full `results`, not just `matches`.** The response includes every row, not only the ones that passed the
  threshold — the same principle as UC1/UC2: showing `insufficient_evidence` (or a near-miss `weak_evidence`) is
  useful information, not something to silently drop. `matches` is a filtered view for convenience.
- **Concurrent, and reuses saved results.** All pairs start at once. `ChatService` caps the LLM calls actually in
  flight, and any (company, criterion) already assessed in UC2/UC3 (or a previous UC4 run) is served from
  `data/analysis/` without an LLM call. In the real-corpus check, a 2-criteria × 8-company run reused the 2 ATOSS
  rows saved by an earlier UC2 run.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.universe import screen_universe

result = await screen_universe(
    ["acquisition_history", "acquisition_strategy"],
    EmbeddingService(),
    ChatService(),
    top_k=3,
    min_assessment="moderate_evidence",  # default
)
# {"criteria": [{"criterion": "acquisition_history", "dimension": "buy_and_build", "polarity": "positive", ...}, ...],
#  "min_assessment": "moderate_evidence",
#  "results": [{"company_id": "nemetschek", "criterion": "acquisition_history", "assessment": "strong_evidence",
#               "meets_threshold": true, "cached": false, ...}, ...],
#  "matches": [...only rows with meets_threshold = true...]}
```

Through the API:

```bash
curl -X POST http://localhost:8000/screen/universe \
  -H "content-type: application/json" \
  -d '{"criteria": ["acquisition_history", "acquisition_strategy"], "top_k": 3, "min_assessment": "moderate_evidence"}'
```

Unknown `criteria` ids, an empty list, or an invalid `min_assessment` value return `400` with a message
explaining why (via `KeyError`/`ValueError` from `screen_universe()`, caught in `app/api/screening.py`). Before
this change, the request body used a single `"criterion": "..."` field. It is now `"criteria": [...]`.

### Running it

Assumes the shared setup from [architecture.md](architecture.md), with the database already populated
([ingestion.md](ingestion.md)) across more than one company, and an `LLM_PROVIDER`/API key set, same as UC2
above.

```bash
python3 - <<'PY'
import asyncio
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.universe import screen_universe

async def main():
    result = await screen_universe(["recurring_revenue"], EmbeddingService(), ChatService(), top_k=3)
    for r in result["results"]:
        print(r["company_id"], r["criterion"], "->", r["assessment"])
    print("matches:", [r["company_id"] for r in result["matches"]])

asyncio.run(main())
PY
```

**Tests:**

```bash
python3 -m pytest tests/test_universe.py -q
```

- `ScreenUniverseValidationTestCase` — pure unit test: unknown criterion ids, an empty list and an invalid
  `min_assessment` all raise, no Postgres or LLM involved.
- `ScreenUniverseFilteringTestCase` — pure unit test: `list_companies_with_documents` and `screen_criterion` are
  both patched, checking the threshold flag/`matches` for one criterion, and that two criteria (one positive, one
  risk) give one row per (company, criterion) with the right `polarity`.
- `ScreenUniverseWorkflowTestCase` — needs Postgres; patches `list_companies_with_documents` to return a fake
  `demo` company and runs the real `screen_criterion` retrieval + a dummy chat service against it, same pattern
  as `ScreenCompanyWorkflowTestCase` in UC2 above.

### Deliberately not built yet

- a combined per-company verdict or ranking across several criteria — see the design note above
- free-text -> criterion id matching — the caller must know the criterion ids today (see
  `config/screening_config.yaml`; the UI offers them as a multi-select)
