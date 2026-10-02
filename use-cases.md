# Use cases

How the four use cases from [project-plan.md](project-plan.md) are implemented, in the order they were built:
asking a single question, generating a full screening report for one company, comparing two companies, and
screening the whole portfolio against one criterion. All four share the same retrieval stage — see
[architecture.md](architecture.md) for the overall data flow and [retrieval.md](retrieval.md) for how a question
becomes ranked evidence chunks — and build on each other: UC2 loops UC1's graph shape once per criterion, UC3 and
UC4 both reuse UC2's per-company/per-criterion functions rather than introducing new retrieval or prompting logic.

## UC1: Ask

How a question turns into an answer with citations. This is UC1 from `project-plan.md`: basic RAG question
answering, one company at a time.

### What it does

```
question
    │
    ▼
retrieve(question, company_id, top_k)   -- same retrieval stage as before, unchanged
    │
    ▼
no chunks found? -> "Insufficient evidence" answer, LLM is never called
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
- **No LLM call when there's no evidence.** If `retrieve` returns zero chunks, `generate_node` short-circuits to
  a fixed `"Insufficient evidence..."` answer instead of prompting the model. This is the same principle
  `framework.md` describes for screening: saying "I don't know based on the documents I have" is a correct
  answer, not a failure.
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
load config/screening_config.yaml -> list of criteria (each tagged with a dimension)
    │
    ▼
for each criterion, in order:
    │
    ▼
    retrieve(criterion.question, company_id, top_k)   -- same retrieval stage as UC1
    │
    ▼
    no chunks found? -> assessment = insufficient_evidence, LLM is never called
    │
    ▼
    build a numbered-sources prompt, ask the LLM to return
    {"assessment": ..., "rationale": "...[1]..."} as JSON
    │
    ▼
    parse the JSON (tolerating code fences / stray text); anything that
    doesn't parse into a known assessment level also becomes insufficient_evidence
    │
    ▼
group the per-criterion results by dimension
    │
    ▼
{ company_id, dimensions: [{ dimension, description, criteria: [{criterion, question,
  assessment, rationale, sources}, ...] }, ...] }
```

Implemented in `app/workflows/screening.py`:

- `load_screening_config()` reads `config/screening_config.yaml` into a flat list of `Criterion` (id, question,
  dimension, dimension description).
- `build_screen_criterion_graph()` is a two-node [LangGraph](https://langchain-ai.github.io/langgraph/) graph —
  `retrieve` → `assess` — the same shape as UC1's `retrieve` → `generate`, with `assess` producing a classified
  assessment instead of free text.
- `screen_criterion()` runs that graph once for one criterion.
- `screen_company()` loads the config and calls `screen_criterion()` once per criterion, sequentially, grouping
  the results by dimension. An optional `dimension` parameter filters the config down to one dimension before
  looping — added for UC3 (Compare) below, which otherwise doesn't need any change to this workflow.

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
- **Sequential, not parallel, across criteria.** `screen_company()` awaits each `screen_criterion()` call one at
  a time, matching the pseudocode in `project-plan.md`. `config/screening_config.yaml` currently has ~19
  criteria, each triggering one retrieval and one LLM call; running them concurrently would be faster but risks
  tripping free-tier LLM rate limits (see the Groq/Google notes above). This is the simplest option that works
  reliably today — worth revisiting with a concurrency limit if screening a full report gets too slow.
- **Grouped by dimension in the response, not a flat list.** The report shape in `project-plan.md`'s UC2 mockup
  is organized by dimension (Business Quality, Growth, ...), so `screen_company()` returns
  `dimensions: [{dimension, description, criteria: [...]}]` instead of one flat array the caller would have to
  group itself.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company

result = await screen_company("atoss", EmbeddingService(), ChatService(), top_k=3)
# {"company_id": "atoss", "dimensions": [
#   {"dimension": "business_quality", "description": "...", "criteria": [
#     {"criterion": "recurring_revenue", "question": "...", "assessment": "strong_evidence",
#      "rationale": "...[1]...", "sources": [...]},
#     ...
#   ]},
#   ...
# ]}
```

A single criterion can be assessed directly with `screen_criterion(criterion, company_id, embedding_service,
chat_service, top_k)`, where `criterion` is one `Criterion` from `load_screening_config()`.

Through the API:

```bash
curl -X POST http://localhost:8000/screen \
  -H "content-type: application/json" \
  -d '{"company_id": "atoss", "top_k": 3}'
```

### Configuration

`config/screening_config.yaml` defines the framework: a `screening_dimensions` mapping, each dimension with a
`description` and a `criteria` mapping of `criterion_id -> {question}`. Adding a criterion or a whole dimension
is a config change only — no code change needed, since `load_screening_config()` reads the structure generically.

Same LLM/embedding configuration as UC1 above (`LLM_PROVIDER`, `LLM_MODEL`, provider API keys) — screening reuses
the same `EmbeddingService` / `ChatService`.

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
  loader and the JSON-parsing fallbacks (clean JSON, code-fenced JSON, invalid JSON, out-of-enum value).
- `ScreenCriterionNoEvidenceTestCase` — patches `retrieve` to return no chunks and checks the LLM is never
  called, same pattern as `AskNoEvidenceTestCase` in `test_ask.py`.
- `ScreenCompanyWorkflowTestCase` — needs Postgres; inserts a hand-picked chunk for a fake `demo` company and
  checks a dummy chat service's fixed JSON reply is parsed and grouped by dimension correctly.
- `ScreenCompanyDimensionFilterTestCase` — pure unit test: an unknown `dimension` id raises, and a known
  dimension narrows the loop to just that dimension's criteria (patches `screen_criterion`).

### Deliberately not built yet

- the Streamlit UI (Phase 6) — `screen_company()` and `POST /screen` are the data source for it
- golden-question evaluation of screening output (Phase 7) — `test_screening.py` covers the workflow's code
  paths, not evidence/groundedness/citation quality against a verified set of questions
- concurrency limits on the per-criterion loop — currently sequential; see the design note above

## UC3: Compare

How the same screening criteria get applied to two companies side by side, to answer questions like
"Nemetschek vs. ATOSS on recurring revenue" instead of reading two separate reports and comparing them by hand.
This is UC3 from `project-plan.md`.

### What it does

```
company_a_id, company_b_id, optional dimension (e.g. "growth")
    │
    ▼
screen_company(company_a_id, ..., dimension=dimension)   -- unchanged, just an optional filter
screen_company(company_b_id, ..., dimension=dimension)   -- same, run for the second company
    │
    ▼
walk company_a's dimensions/criteria; for each criterion, look up the matching
criterion (same id) in company_b's results
    │
    ▼
{ company_a, company_b, dimension, dimensions: [{ dimension, description, criteria: [
  { criterion, question, company_a: {assessment, rationale, sources},
    company_b: {assessment, rationale, sources} }, ... ] }, ...] }
```

Implemented in `app/workflows/compare.py`:

- `compare_companies()` is the only function here. It does not introduce a new LangGraph graph or a new
  retrieve/assess step — it calls `screen_company()` once per company (sequentially) and merges the two
  per-company reports into one side-by-side structure, keyed by criterion id.
- `screen_company()` in `screening.py` (UC2 above) gained an optional `dimension` parameter for this: when set,
  it filters `load_screening_config()`'s criteria to that one dimension before looping, instead of assessing all
  ~19 criteria when the caller only wants e.g. "Growth". An unknown dimension id raises `KeyError`, listing the
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
  different chunks, so there's no shared retrieval step to merge the way UC1/UC2 share one query — the two
  `screen_company()` calls are independent and sequential, same rate-limit reasoning as the loops in UC2/UC4.
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
  -d '{"company_a": "nemetschek", "company_b": "atoss", "top_k": 3, "dimension": "growth"}'
```

An unknown `dimension` id returns `400` with a message listing the valid dimension ids (via `KeyError` from
`screen_company()`, caught in `app/api/screening.py`).

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

- the Streamlit UI (Phase 6) — `compare_companies()` and `POST /compare` are the data source for it
- comparing more than two companies at once — `project-plan.md`'s UC3 mockup is explicitly two companies
  ("Company A vs. Company B"); comparing a larger set is closer to UC4's universe screening, which already
  exists for the single-criterion case
- combining multiple criteria into one dimension-level "who's ahead" verdict — same reasoning as UC2/UC4: left
  to the caller, not baked into the workflow

## UC4: Universe screening

How one screening criterion gets evaluated across every ingested company at once, to answer questions like
"which companies have strong evidence of recurring revenue?" instead of "tell me about ATOSS." This is UC4
from `project-plan.md` — the stretch feature, explicitly "mostly reuse of UC2."

### What it does

```
criterion_id (e.g. "recurring_revenue"), min_assessment (e.g. "moderate_evidence")
    │
    ▼
look up the criterion in config/screening_config.yaml -> unknown id raises KeyError
    │
    ▼
list_companies_with_documents() -> every company_id with at least one ingested document
    │
    ▼
for each company, in order:
    │
    ▼
    screen_criterion(criterion, company_id, ...)   -- same per-criterion graph as UC2, unchanged
    │
    ▼
filter to companies whose assessment is at least as strong as min_assessment
    │
    ▼
{ criterion, question, min_assessment, results: [{company_id, criterion, dimension,
  question, assessment, rationale, sources}, ...], matches: [...same shape, filtered...] }
```

Implemented in `app/workflows/universe.py`:

- `screen_universe()` is the only function here. It does not introduce a new LangGraph graph — it loops
  `screen_criterion()` from `screening.py` once per company, exactly like `screen_company()` loops it once per
  criterion for one company.
- `list_companies_with_documents()` (in `app/db/repository.py`) returns the distinct `company_id`s that have at
  least one row in `documents`, which is the actual screenable universe — not every id in `companies.yaml`,
  some of which may not have been ingested yet.

### Design decisions

- **One criterion per call, not a free-text query.** `project-plan.md`'s UC4 examples ("find companies with
  strong evidence of recurring revenue") map directly onto one criterion id from `screening_config.yaml`. Rather
  than adding an NLP layer to match free text to the nearest criterion, the caller passes the criterion id
  directly — it's already a stable, known vocabulary shared with UC2. Mapping a free-text question to a
  criterion id would be a reasonable future layer on top, not a change to this function.
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
- **Full `results`, not just `matches`.** The response includes every company's assessment, not only the ones
  that passed the threshold — the same principle as UC1/UC2: showing `insufficient_evidence` (or a near-miss
  `weak_evidence`) is useful information, not something to silently drop. `matches` is a filtered view for
  convenience, computed from `results`, not a separate query.
- **Single criterion, not a whole dimension, per call.** `framework.md`'s "buy & build" example combines four
  criteria (`acquisition_history`, `fragmented_market`, `acquisition_strategy`, `integration_capability`) into
  one qualitative judgment. That combination is left to the caller — call `screen_universe()` once per criterion
  in the dimension and combine the `matches` client-side — rather than baking a second aggregation rule
  (e.g. "all four must be at least moderate") into this function, which would be guessing at a judgment call
  that belongs in the UI/consumer, not the workflow layer.
- **Sequential across companies**, same reasoning as the sequential loop over criteria in `screen_company()`:
  simplicity and free-tier LLM rate limits over raw speed. For 8 ingested companies this is already the slower
  axis than the ~19 criteria in `screen_company()`, so a concurrency limit (e.g. `asyncio.Semaphore`) is the
  first thing to add if this becomes too slow.

### How it's used

```python
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.universe import screen_universe

result = await screen_universe(
    "recurring_revenue",
    EmbeddingService(),
    ChatService(),
    top_k=3,
    min_assessment="moderate_evidence",  # default
)
# {"criterion": "recurring_revenue", "question": "...", "min_assessment": "moderate_evidence",
#  "results": [{"company_id": "atoss", "assessment": "strong_evidence", ...}, ...],
#  "matches": [...only companies with moderate_evidence or stronger...]}
```

Through the API:

```bash
curl -X POST http://localhost:8000/screen/universe \
  -H "content-type: application/json" \
  -d '{"criterion": "recurring_revenue", "top_k": 3, "min_assessment": "moderate_evidence"}'
```

An unknown `criterion` id or an invalid `min_assessment` value returns `400` with a message listing the valid
criterion ids (via `KeyError`/`ValueError` from `screen_universe()`, caught in `app/api/screening.py`).

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
    result = await screen_universe("recurring_revenue", EmbeddingService(), ChatService(), top_k=3)
    for r in result["results"]:
        print(r["company_id"], "->", r["assessment"])
    print("matches:", [r["company_id"] for r in result["matches"]])

asyncio.run(main())
PY
```

**Tests:**

```bash
python3 -m pytest tests/test_universe.py -q
```

- `ScreenUniverseValidationTestCase` — pure unit test: unknown criterion id and invalid `min_assessment` both
  raise, no Postgres or LLM involved.
- `ScreenUniverseFilteringTestCase` — pure unit test: `list_companies_with_documents` and `screen_criterion` are
  both patched, checking only the threshold-filtering logic in isolation.
- `ScreenUniverseWorkflowTestCase` — needs Postgres; patches `list_companies_with_documents` to return a fake
  `demo` company and runs the real `screen_criterion` retrieval + a dummy chat service against it, same pattern
  as `ScreenCompanyWorkflowTestCase` in UC2 above.

### Deliberately not built yet

- combining multiple criteria into one dimension-level judgment (e.g. "buy & build" as a single verdict) — call
  `screen_universe()` once per criterion in the dimension and combine client-side; see the design note above
- free-text -> criterion id matching — the caller must know the criterion id today (see `config/screening_config.yaml`)
- concurrency limits on the per-company loop — currently sequential; see the design note above
