# Universe screening

How one screening criterion gets evaluated across every ingested company at once, to answer questions like
"which companies have strong evidence of recurring revenue?" instead of "tell me about ATOSS." See
[screening.md](screening.md) for the per-company workflow this one loops, and [architecture.md](architecture.md)
for the overall data flow. This is UC4 from [project-plan.md](project-plan.md) — the stretch feature, explicitly
"mostly reuse of UC2."

## What it does

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
    screen_criterion(criterion, company_id, ...)   -- same per-criterion graph as screening.py, unchanged
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

## Design decisions

- **One criterion per call, not a free-text query.** `project-plan.md`'s UC4 examples ("find companies with
  strong evidence of recurring revenue") map directly onto one criterion id from `screening_config.yaml`. Rather
  than adding an NLP layer to match free text to the nearest criterion, the caller passes the criterion id
  directly — it's already a stable, known vocabulary shared with `screening.md`. Mapping a free-text question to
  a criterion id would be a reasonable future layer on top, not a change to this function.
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
  that passed the threshold — the same principle as `ask.md`/`screening.md`: showing `insufficient_evidence`
  (or a near-miss `weak_evidence`) is useful information, not something to silently drop. `matches` is a
  filtered view for convenience, computed from `results`, not a separate query.
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

## How it's used

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

## Running it

Assumes the shared setup from [architecture.md](architecture.md), with the database already populated
([ingestion.md](ingestion.md)) across more than one company, and an `LLM_PROVIDER`/API key set, same as
[screening.md](screening.md).

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
  as `ScreenCompanyWorkflowTestCase` in `test_screening.py`.

## Deliberately not built yet

- company comparison (UC3) — still not built; it compares two specific companies rather than scanning the whole
  universe, so it doesn't reuse `screen_universe()`, only `screen_company()`
- combining multiple criteria into one dimension-level judgment (e.g. "buy & build" as a single verdict) — call
  `screen_universe()` once per criterion in the dimension and combine client-side; see the design note above
- free-text -> criterion id matching — the caller must know the criterion id today (see `config/screening_config.yaml`)
- concurrency limits on the per-company loop — currently sequential; see the design note above
