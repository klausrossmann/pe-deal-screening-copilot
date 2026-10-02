# Retrieval

How a question becomes a ranked list of evidence chunks. See [architecture.md](architecture.md) for the overall data flow and database schema; see [ingestion.md](ingestion.md) for how the chunks got there in the first place.

## What it does

```
question
    │
    ▼
embed the question (same provider/model used during ingestion; each distinct query is embedded once per process)
    │
    ▼
pgvector cosine-distance search over chunks, optionally WHERE company_id = ...
    │                                         and WHERE distance <= max_distance
    ▼
top_k chunks, closest first, each with its chunk_id, page, document, and distance
```

Implemented in `app/retrieval/retriever.py` as two functions:

| Function | Used by | What it does |
| --- | --- | --- |
| `retrieve(query, ...)` | UC1 Ask | one query → one pgvector search |
| `retrieve_multi(queries, ...)` | UC2–UC4 screening | one search per query, merged: each chunk appears once, at its best (smallest) distance, then cut to `top_k` |

Retrieval itself never calls an LLM and never judges evidence strength — that's the workflows' job (see [use-cases.md](use-cases.md)).

## Design decisions

- **Cosine distance.** The default local model is used with normalized embeddings, which is exactly what cosine distance is built for; OpenAI and Google embeddings are also comparable this way.
- **Same embedding service as ingestion.** The query is embedded with the same `EmbeddingService` instance used to embed the chunks — mixing providers or models between query and corpus would make the distances meaningless. That's why `retrieve()` takes an `embedding_service` argument instead of constructing its own.
- **Filtering happens in the SQL `WHERE`, not in Python.** A company filter is applied before the distance is computed, so a query never has to load the whole table into memory. Comparing two companies (a future use case) becomes two independent, equally filtered retrievals rather than one mixed call.
- **"Closest first" only.** Deciding how strong the evidence is belongs to the LLM step later, not to retrieval.
- **An optional distance cutoff (`max_distance`).** Without it, a company with any documents *always* returns
  `top_k` chunks, even for a question its documents don't cover — so the workflows' "no evidence → skip the LLM"
  shortcut could never trigger. With a cutoff, chunks that are too far away are dropped in the SQL `WHERE`, an
  off-topic question can return nothing, and the LLM is never shown unrelated text. See *Choosing the cutoff*
  below.
- **Several phrasings per question (`retrieve_multi`).** A question like *"What evidence is there of organic
  growth?"* doesn't look much like the sentence that answers it (*"revenue grew 9% at constant currency"*).
  Screening criteria therefore also list `retrieval_queries` worded like the documents themselves (see
  `config/screening_config.yaml`). Each phrasing is searched separately and the results are merged. This cut the
  average top-1 distance for `organic_growth` from 0.65 to 0.43, and for `geographic_concentration` from 0.66 to
  0.47, across the 8 ingested companies.
- **Every chunk carries its `chunk_id`.** Needed to merge results from several queries without duplicates, and
  saved with each screening result so a citation can always be traced back to the exact chunk.
- **Nothing blocks the event loop.** The database driver and the embedding model are synchronous, so both run in a
  worker thread (`asyncio.to_thread`). That's what lets the workflows run many searches concurrently.
- **Query embeddings are cached.** UC4 asks the same criterion queries for every company, so `EmbeddingService`
  keeps an in-memory cache of the last 1,024 query embeddings. The local model is not thread-safe (concurrent
  `encode()` calls crash the process on macOS), so local-model calls take turns behind a lock — cheap, since each
  query is embedded only once anyway.

## How it's queried

Conceptually, `retrieve(query, embedding_service, company_id=None, top_k=5, max_distance=None)` runs:

```sql
SELECT chunks.id, chunks.company_id, chunks.page_number, chunks.content, chunks.metadata,
       documents.title, documents.file_name,
       chunks.embedding <=> :query_embedding AS distance
FROM chunks
JOIN documents ON documents.id = chunks.document_id
WHERE chunks.company_id = :company_id                 -- only when a company_id is given
  AND chunks.embedding <=> :query_embedding <= :max   -- only when a max_distance is given
ORDER BY distance
LIMIT :top_k;
```

`<=>` is pgvector's cosine-distance operator — a smaller `distance` means a closer match (0 = identical meaning,
1 = unrelated).

## Choosing the cutoff

The API reads the cutoff from `RETRIEVAL_MAX_DISTANCE` (unset = no cutoff; functions called directly default to
no cutoff too). The right value depends on the embedding model, so it was measured on the real corpus with the
default `all-MiniLM-L6-v2`:

| Query | Best-matching chunk per company (cosine distance) |
| --- | --- |
| Screening criteria (question + `retrieval_queries`) | 0.19 – 0.62 |
| Off-topic: *"chocolate cake recipe"* | 0.81 – 0.88 |
| Off-topic: *"football world cup results"* | 0.61 – 0.77 |

The recommended `RETRIEVAL_MAX_DISTANCE=0.70` (in `.env.example`) keeps every best match for every criterion and
company, plus some margin for freely worded UC1 questions (some plainly relevant chunks sat at ~0.70), while
dropping clearly unrelated text such as website language menus (0.76–0.82). It is a coarse filter, not a
relevance judgement. **Re-measure if you switch the embedding model**: OpenAI/Google distances are on a
different scale.

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

`RetrieveTestCase` inserts a couple of chunks with hand-picked embeddings for fake `demo`/`demo2` companies (no real model needed, so it's fast and deterministic), then checks that:

- filtering by company excludes the other company's rows, and the closer chunk is returned first
- `max_distance` drops a chunk that is too far away
- `retrieve_multi` merges two queries and returns each chunk once

It cleans up its own rows, so it's safe to run against the real database.

## Deliberately not built yet

- reranking or hybrid keyword+vector search — the per-criterion `retrieval_queries` already fixed the cases where plain cosine similarity missed obvious matches
- a per-criterion distance cutoff — one global value covers the measured range today
- hierarchical ("small-to-big" / parent-document) retrieval, where a small chunk is matched but a larger surrounding unit (its page, or the whole document) is returned as context — this would give the future LLM step more coherent context than one isolated chunk, but there's no consumer that needs it yet. Build it once the LLM synthesis step shows single-chunk context genuinely isn't enough, not before. See [ingestion.md](ingestion.md) for why chunks are already sized at the embedding model's limit rather than made smaller on purpose.
