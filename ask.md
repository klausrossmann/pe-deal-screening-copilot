# Ask

How a question turns into an answer with citations. See [architecture.md](architecture.md) for the overall data
flow and project structure; see [retrieval.md](retrieval.md) for the stage this builds on (retrieval stops at
ranked chunks — no LLM call, no synthesis). This is UC1 from [project-plan.md](project-plan.md): basic RAG
question answering, one company at a time.

## What it does

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
`retrieve` → `generate`. LangGraph is used here, not in ingestion or retrieval, because `architecture.md` already
called this out as a separate orchestration layer for multi-step `ask` / `screen` / `compare` workflows — plain
function calls are enough for the single-query stages, but screening (Phase 5) will reuse this same graph shape
once per criterion, which is where a graph abstraction starts paying for itself.

## Design decisions

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
  `app/services/embeddings.py`: a provider switch (`LLM_PROVIDER` — `openai` or `google`) over the plain `openai`
  / `google-genai` clients, imported lazily. LangGraph only needs plain Python functions for its nodes, so there
  was no reason to add `langchain-openai` / `langchain-google-genai` just for one `generate(prompt) -> str` call.
  Note this means the LLM service and the embedding service currently use two different Google SDKs:
  `app/services/llm.py` uses the current `google-genai` (`from google import genai`), while
  `app/services/embeddings.py` still uses the deprecated `google-generativeai` — only worth unifying if/when
  `EmbeddingService` is touched for another reason.
- **Google calls retry transient failures with backoff.** The free-tier Flash models occasionally return
  `503 UNAVAILABLE` ("This model is currently experiencing high demand... usually temporary") under load. The
  `genai.Client` is constructed with `http_options=types.HttpOptions(retry_options=types.HttpRetryOptions(...))`
  (5 attempts, exponential backoff from 1s up to 20s) so a transient 503 is retried instead of failing the whole
  `ask()` call. This only helps with genuinely transient errors — a `404` (wrong model name) or a `429` with a
  `0` free-tier quota (wrong pricing tier) will still fail after retrying, since those aren't temporary.

## How it's used

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

## Configuration

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

## Running it

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

## Deliberately not built yet

- the PE Screening workflow itself (Phase 5 / UC2) — looping this same `ask`-shaped graph once per criterion in
  `screening_config.yaml`, with assessment classification (`strong_evidence` / ... / `insufficient_evidence`)
  instead of free-text answers
- company comparison (UC3) and portfolio-wide screening (UC4) — both reuse the screening result, not this stage
- conversation memory / multi-turn follow-up questions — `ask()` is stateless, one question in, one answer out
- streaming responses — the LLM call is a single blocking `generate()`, not a token stream
