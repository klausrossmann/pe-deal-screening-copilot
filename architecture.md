# Architecture

This file explains how the system fits together: the overall data flow, the project layout, and the database schema. It's the shared background for [ingestion.md](ingestion.md), [retrieval.md](retrieval.md), and [use-cases.md](use-cases.md), which each walk through one stage in detail.

## Data flow

```
data/raw/<company>/*.{pdf,html}
        │
        ▼
   Ingestion pipeline        (parse → chunk → enrich → embed)
        │
        ▼
  PostgreSQL + pgvector      (companies, documents, chunks)
        │
        ▼
   Retrieval                 (embed query → cosine search, optionally per company and within a distance cutoff;
        │                     screening searches several phrasings per criterion and merges them)
        ├─────────────────────────────┐
        ▼                             ▼
   Ask workflow (LangGraph)     Screening workflow (LangGraph)
   (retrieve → LLM answer        (retrieve → LLM assessment per criterion
   with citations)                in config/screening_config.yaml)  ◄──►  data/analysis/  (saved results,
        │                             │                                   reused until their inputs change)
        ▼                             ▼
  answer + source citations    report grouped by dimension, each criterion
                                classified strong/moderate/weak/insufficient evidence
                                (risk criteria: strong = the risk is present)
                                       │
                            ┌──────────┴──────────┐
                            ▼                     ▼
                     universe workflow      compare workflow
                     (chosen criteria ×      (runs the report for
                     every company)          two companies, merged
                                              by criterion)
```

PostgreSQL is the source of truth for the processed corpus — not the filesystem, not objects held in memory. Anything downstream (an LLM answer, a screening report, a comparison) reads from the database, never from the ingestion run that produced it. The saved screening results in `data/analysis/` are a disposable cache on top: each one records a fingerprint of the documents and settings it came from and is ignored once they change, so deleting the folder never loses anything.

## Responsibilities

| Layer | Responsibility |
| --- | --- |
| FastAPI (`app/main.py`, `app/api/`) | HTTP boundary: trigger ingestion, check status, ask questions |
| Ingestion (`app/ingestion/`) | Turn files under `data/raw/` into rows in Postgres |
| Retrieval (`app/retrieval/`) | Turn a question into ranked evidence chunks |
| Workflows (`app/workflows/`) | Stateful, multi-step flows built with LangGraph on top of retrieval (`ask`, `screen` today, plus `universe` which runs `screen` per criterion across companies, and `compare` which runs `screen` for two companies and merges the results); saved screening results |
| Services (`app/services/`) | Swappable dependencies: embedding provider and LLM provider. One shared instance each per process; `ChatService` also caps concurrent LLM calls (`LLM_MAX_CONCURRENCY`) |
| PostgreSQL + pgvector | Storage and similarity search |

## Project structure

```
app/
├── main.py                  FastAPI app; creates the tables on startup
├── api/
│   ├── ingestion.py         POST /ingestion/all, GET /ingestion/status
│   ├── ask.py               POST /ask
│   └── screening.py         POST /screen, POST /screen/universe, POST /compare
├── db/
│   ├── session.py           database engine + session helper
│   ├── models.py            companies, documents, chunks tables
│   ├── bootstrap.py         creates the pgvector extension and the tables
│   └── repository.py        every database read/write the ingestion needs
├── ingestion/
│   ├── manifest.py          reads config/sources.yaml and config/companies.yaml
│   ├── loader.py            finds a source file on disk and hashes it
│   ├── parser.py            PDF/HTML -> pages
│   ├── chunker.py           pages -> chunks
│   ├── metadata.py          adds company/document metadata to each chunk
│   └── pipeline.py          runs all of the above, per source and for all sources
├── retrieval/
│   └── retriever.py         embeds a query and runs the pgvector search (retrieve, retrieve_multi)
├── workflows/
│   ├── ask.py               LangGraph retrieve -> generate graph (question -> answer + citations)
│   ├── screening.py         LangGraph retrieve -> assess graph, run for every criterion (company -> report)
│   ├── universe.py          runs screen_criterion() for every (company, criterion) pair (criteria -> table)
│   ├── compare.py           runs screen_company() for two companies and merges the results per criterion
│   ├── analysis_cache.py    saves/reuses screening results under data/analysis/
│   └── common.py            source-formatting helpers shared by ask.py and screening.py
├── schemas/
│   └── documents.py         Page and Chunk dataclasses shared across modules
├── services/
│   ├── embeddings.py        embedding provider wrapper (local/OpenAI/Google)
│   └── llm.py               chat/LLM provider wrapper (OpenAI/Google/Groq)
└── ui/
    └── streamlit_app.py     Streamlit UI: Screening/Ask/Compare/Universe tabs, calls the FastAPI endpoints

config/
├── sources.yaml              which documents to ingest (source manifest)
├── companies.yaml            company master data
└── screening_config.yaml     PE screening framework: dimensions (incl. risk polarity), criteria, retrieval queries

data/raw/<company_id>/        the source files themselves
data/analysis/<company_id>/   saved screening results, one JSON per criterion (git-ignored, safe to delete)
tests/                        pytest suite covering ingestion, retrieval, the ask workflow, the screening
                               workflow, saved results, universe screening, and company comparison
docker-compose.yml            local PostgreSQL + pgvector
```

Each module in `app/ingestion/` does exactly one job and can be tested alone; `pipeline.py` is the only place that knows the order they run in. Retrieval is small enough to stay in a single file. `app/workflows/` is where LangGraph-based orchestration lives, kept separate from `retrieval/` because a workflow composes multiple steps (retrieve, then call an LLM) while retrieval itself stays a single pgvector query.

## Database schema

Three tables, defined once in `app/db/models.py` (SQLAlchemy) — there is no separate SQL schema file, so the schema can't drift between two places.

**companies** — one row per company, filled in from `config/companies.yaml` the first time one of its documents is ingested.

| column | notes |
| --- | --- |
| id | e.g. `"atoss"`, primary key |
| name, country, category, ownership, ticker | from companies.yaml |

**documents** — one row per ingested file.

| column | notes |
| --- | --- |
| id | UUID |
| company_id | foreign key to companies |
| document_type, title, year, source_url, file_name | from sources.yaml |
| content_hash | SHA-256 of the file content, **unique** |
| created_at | |

The unique `content_hash` is what makes ingestion idempotent: the database itself refuses a second row for a file whose content hasn't changed, regardless of what it's named.

**chunks** — one row per chunk of text.

| column | notes |
| --- | --- |
| id | UUID |
| document_id, company_id | foreign keys |
| chunk_index, page_number, content | |
| embedding | pgvector column, **no fixed dimension** |
| metadata | JSONB — company/document context duplicated for convenient filtering |
| created_at | |

The embedding column has no fixed size because different providers produce different vector lengths (384 for the default local model, 1536 for OpenAI/Google). Don't mix providers in one database — rebuild with `python3 -m app.db.bootstrap --reset` if you switch.

## Local environment

Both ingestion and retrieval share the same setup:

1. Copy `.env.example` to `.env` and adjust it if needed (database URL, embedding provider, LLM provider, `RETRIEVAL_MAX_DISTANCE`, `LLM_MAX_CONCURRENCY`).
2. Export it into the shell — the app does not load `.env` by itself:
   ```bash
   set -a; source .env; set +a
   ```
3. Start PostgreSQL: `docker compose up -d`
4. Create the tables: `python3 -m app.db.bootstrap` (safe to re-run; add `--reset` to drop and recreate, e.g. after a model or embedding-provider change)
5. Run the tests: `python3 -m pytest tests -q` (needs the Postgres container running; tests only touch rows of fake `demo`/`demo2` companies, so it's safe to run against your real database)

With that in place, continue with [ingestion.md](ingestion.md) to populate the database, then [retrieval.md](retrieval.md) to query it.
