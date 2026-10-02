# Screening

How a company turns into a full PE screening report: one assessment per criterion in the screening
framework, each grounded in retrieved evidence. See [architecture.md](architecture.md) for the overall data
flow; see [ask.md](ask.md) for the single-question workflow this one reuses the shape of. This is UC2 from
[project-plan.md](project-plan.md), Phase 5 — the hero feature of the project.

## What it does

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
    retrieve(criterion.question, company_id, top_k)   -- same retrieval stage as `ask`
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
  `retrieve` → `assess` — the same shape as `ask`'s `retrieve` → `generate`, with `assess` producing a
  classified assessment instead of free text.
- `screen_criterion()` runs that graph once for one criterion.
- `screen_company()` loads the config and calls `screen_criterion()` once per criterion, sequentially, grouping
  the results by dimension.

## Design decisions

- **Reuses the `ask` graph shape, not the `ask` graph itself.** `architecture.md` and `ask.md` already called
  this out: screening loops a `retrieve` → LLM graph once per criterion. The LLM step is different enough
  (structured classification vs. free-text answer with citations) that it's a separate `assess` node and prompt,
  not a parameterized reuse of `generate_node`. `format_sources_block()` and `to_source_citations()` — the two
  bits that *are* identical — were pulled out into `app/workflows/common.py` so `ask.py` and `screening.py` share
  them instead of duplicating the formatting logic.
- **Evidence classification, not a score.** `framework.md` is explicit about this: the LLM is asked to classify
  evidence into `strong_evidence` / `moderate_evidence` / `weak_evidence` / `insufficient_evidence`, never to
  invent a numeric score. The prompt enforces this with an exact enum of allowed values.
- **No LLM call when there's no evidence**, same principle as `ask.py`: if `retrieve` returns zero chunks for a
  criterion, `assess_node` short-circuits straight to `insufficient_evidence` instead of prompting the model to
  guess.
- **Structured output via prompted JSON, not a provider-specific schema API.** `ChatService.generate()` is a
  single `prompt -> str` call shared across `openai`/`google`/`groq` (see `ask.md`), so there's no common
  structured-output API to hook into without provider-specific branching. Instead, the prompt asks for a JSON
  object with exactly two keys, and `_parse_assessment()` extracts the first `{...}` block with a regex before
  calling `json.loads` — tolerant of a model wrapping the JSON in a code fence or a sentence of preamble.
- **Unparseable or out-of-enum output still produces a usable result.** If the LLM's reply isn't valid JSON, or
  its `assessment` value isn't one of the four allowed levels, `_parse_assessment()` falls back to
  `insufficient_evidence` with an explanatory rationale rather than raising. A broken LLM response degrades to
  "we don't have conclusive evidence," not a 500 error for the whole report.
- **Sequential, not parallel, across criteria.** `screen_company()` awaits each `screen_criterion()` call one at
  a time, matching the pseudocode in `project-plan.md`. `config/screening_config.yaml` currently has ~19
  criteria, each triggering one retrieval and one LLM call; running them concurrently would be faster but risks
  tripping free-tier LLM rate limits (see the Groq/Google notes in `ask.md`). This is the simplest option that
  works reliably today — worth revisiting with a concurrency limit if screening a full report gets too slow.
- **Grouped by dimension in the response, not a flat list.** The report shape in `project-plan.md`'s UC2 mockup
  is organized by dimension (Business Quality, Growth, ...), so `screen_company()` returns
  `dimensions: [{dimension, description, criteria: [...]}]` instead of one flat array the caller would have to
  group itself.

## How it's used

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

## Configuration

`config/screening_config.yaml` defines the framework: a `screening_dimensions` mapping, each dimension with a
`description` and a `criteria` mapping of `criterion_id -> {question}`. Adding a criterion or a whole dimension
is a config change only — no code change needed, since `load_screening_config()` reads the structure generically.

Same LLM/embedding configuration as `ask.md` (`LLM_PROVIDER`, `LLM_MODEL`, provider API keys) — screening reuses
the same `EmbeddingService` / `ChatService`.

## Running it

Assumes the shared setup from [architecture.md](architecture.md), with the database already populated
([ingestion.md](ingestion.md)) and an `LLM_PROVIDER`/API key set, same as [ask.md](ask.md).

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

## Deliberately not built yet

- company comparison (UC3) — applying the same criteria to two companies side by side; reuses `screen_company()`
  for each company, not this stage
- the Streamlit UI (Phase 6) — `screen_company()` and `POST /screen` are the data source for it
- golden-question evaluation of screening output (Phase 7) — `test_screening.py` covers the workflow's code
  paths, not evidence/groundedness/citation quality against a verified set of questions
- concurrency limits on the per-criterion loop — currently sequential; see the design note above

Portfolio-wide screening (UC4) — running one criterion across every company — is now built; see
[universe.md](universe.md).
