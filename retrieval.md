# Retrieval

How a question becomes a ranked list of evidence chunks. See [architecture.md](architecture.md) for the overall data flow and database schema; see [ingestion.md](ingestion.md) for how the chunks got there in the first place.

## What it does

```
question
    │
    ▼
embed the question (same provider/model used during ingestion)
    │
    ▼
pgvector cosine-distance search over chunks, optionally WHERE company_id = ...
    │
    ▼
top_k chunks, closest first, each with its page, document, and distance
```

That's the whole stage — one query, implemented in `app/retrieval/retriever.py`. Per the project plan, this is deliberately where it stops for now: no LLM call, no synthesis, no "strong/moderate/weak evidence" judgement yet. If the raw chunks returned here already look relevant for a question like *"What evidence is there of recurring revenue at ATOSS?"*, the ingestion architecture is proven and it's safe to build the LLM step on top of it.

## Design decisions

- **Cosine distance.** The default local model is used with normalized embeddings, which is exactly what cosine distance is built for; OpenAI and Google embeddings are also comparable this way.
- **Same embedding service as ingestion.** The query is embedded with the same `EmbeddingService` instance used to embed the chunks — mixing providers or models between query and corpus would make the distances meaningless. That's why `retrieve()` takes an `embedding_service` argument instead of constructing its own.
- **Filtering happens in the SQL `WHERE`, not in Python.** A company filter is applied before the distance is computed, so a query never has to load the whole table into memory. Comparing two companies (a future use case) becomes two independent, equally filtered retrievals rather than one mixed call.
- **"Closest first" only.** Deciding how strong the evidence is belongs to the LLM step later, not to retrieval.

## How it's queried

Conceptually, `retrieve(query, embedding_service, company_id=None, top_k=5)` runs:

```sql
SELECT chunks.company_id, chunks.page_number, chunks.content, chunks.metadata,
       documents.title, documents.file_name,
       chunks.embedding <=> :query_embedding AS distance
FROM chunks
JOIN documents ON documents.id = chunks.document_id
WHERE chunks.company_id = :company_id   -- only when a company_id is given
ORDER BY distance
LIMIT :top_k;
```

`<=>` is pgvector's cosine-distance operator — a smaller `distance` means a closer match. There's no fixed "good" threshold; read the number next to the actual chunk text.

## Running it

Assumes the shared setup from [architecture.md](architecture.md), and that [ingestion.md](ingestion.md) has already populated the database.

**Manually, against the real corpus:**

```bash
python3 - <<'PY'
import asyncio
from app.retrieval.retriever import retrieve
from app.services.embeddings import EmbeddingService

async def main():
    service = EmbeddingService()
    results = await retrieve(
        "What evidence is there of recurring revenue?",
        service,
        company_id="atoss",
        top_k=5,
    )
    for r in results:
        print(round(r["distance"], 4), r["file_name"], "p.", r["page"], "-", r["content"][:120])

asyncio.run(main())
PY
```

Expect five chunks, ordered by increasing distance, actually about recurring revenue. If they look unrelated, the problem is almost always upstream in ingestion (chunking, metadata, or the embedding model) — see the ingestion troubleshooting table first.

**Tests:**

```bash
python3 -m pytest tests -q
```

`RetrieveTestCase` inserts a couple of chunks with hand-picked embeddings for fake `demo`/`demo2` companies (no real model needed, so it's fast and deterministic), then checks that filtering by company excludes the other company's rows and that the closer chunk is returned first. It cleans up its own rows, so it's safe to run against the real database.

## Deliberately not built yet

- an LLM call that turns chunks into an answer with citations — the next phase (see `project-plan.md`, UC2)
- a `/retrieve` or `/ask` API endpoint — tested manually first, same as ingestion was before `POST /ingestion/all` existed
- reranking or hybrid keyword+vector search — only worth adding if plain cosine similarity turns out to miss obvious matches
- hierarchical ("small-to-big" / parent-document) retrieval, where a small chunk is matched but a larger surrounding unit (its page, or the whole document) is returned as context — this would give the future LLM step more coherent context than one isolated chunk, but there's no consumer that needs it yet. Build it once the LLM synthesis step shows single-chunk context genuinely isn't enough, not before. See [ingestion.md](ingestion.md) for why chunks are already sized at the embedding model's limit rather than made smaller on purpose.
