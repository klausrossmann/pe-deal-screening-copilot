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
Dockerfile                     shared image for the api and ui containers (see "Running with Docker" below)
docker-compose.yml            PostgreSQL + pgvector, plus the api and ui containers
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

1. Copy `.env.example` to `.env` and adjust it if needed (database URL, embedding provider, LLM provider, `RETRIEVAL_MAX_DISTANCE`, `LLM_MAX_CONCURRENCY`). It's loaded automatically on the first `import app...` (via `python-dotenv` in `app/__init__.py`), so `uvicorn`, `pytest` and `python -m app...` all pick it up without a manual `source` step. A variable already set in the shell still wins over `.env`.
2. Start PostgreSQL: `docker compose up -d`
3. Create the tables: `python3 -m app.db.bootstrap` (safe to re-run; add `--reset` to drop and recreate, e.g. after a model or embedding-provider change)
4. Run the tests: `python3 -m pytest tests -q` (needs the Postgres container running; tests only touch rows of fake `demo`/`demo2` companies, so it's safe to run against your real database)

With that in place, continue with [ingestion.md](ingestion.md) to populate the database, then [retrieval.md](retrieval.md) to query it.

## Running with Docker

`docker compose up -d --build` starts three containers from the same image (built from the root `Dockerfile`):
`postgres`, `api` (`uvicorn`, port 8000) and `ui` (`streamlit`, port 8501). A `.env` file must exist first (step 1
above) — `docker compose` reads it via `env_file`. Two variables are overridden in `docker-compose.yml` itself
so the containers can reach each other by service name instead of `localhost`: `POSTGRES_URL` (api/ui -> postgres)
and `API_BASE_URL` (ui -> api). `data/analysis/` is bind-mounted into the `api` container so saved screening
results survive container restarts/rebuilds; `data/raw/` and `config/` are baked into the image, so rebuild
(`docker compose up -d --build`) after changing either. Run ingestion/tests the same way as above, just via
`docker compose exec api ...` instead of a local Python environment.

## Deploying to AWS

A low-cost layout: one EC2 instance (free-tier eligible `t3.micro`/`t2.micro`) running the `api` and `ui`
containers via `docker-compose.prod.yml`, plus a managed RDS PostgreSQL instance (free-tier eligible
`db.t3.micro`/`db.t4g.micro`, engine version 16.x or 15.4+ for `pgvector` support) instead of the local
`postgres` container. `docker-compose.prod.yml` differs from `docker-compose.yml` only by dropping the
local `postgres` service — `POSTGRES_URL` in `.env` must then point at the RDS endpoint instead of
`localhost`/`postgres`.

> New AWS accounts (since the 2024 Free Tier change) get **$100–200 in credits valid for 6 months**, not
> the older "12 months of free EC2/RDS hours" — check the Billing console's Free Tier page for what
> actually applies to your account. A `t3.micro` EC2 instance + `db.t3.micro` RDS instance running 24/7
> cost only a few dollars a month even outside the credit, but set an AWS Budget alert (e.g. $5) as a
> safety net, and stop (not terminate) both resources when you're not actively using the app to conserve
> credit — EBS/RDS storage keeps billing a few cents a month while stopped, compute does not.

Steps:

1. **IAM**: create an IAM user (or use IAM Identity Center) with programmatic access and run `aws configure`
   locally so the AWS CLI can create resources.
2. **RDS**: create a `db.t3.micro`/`db.t4g.micro` PostgreSQL 16.x instance (20 GB storage, not publicly
   accessible), in the same VPC as the EC2 instance. After it's up, connect once (e.g. via an SSH tunnel
   through the EC2 instance) and confirm `CREATE EXTENSION vector;` succeeds — this is what
   `python3 -m app.db.bootstrap` runs automatically, so no manual SQL is needed beyond that check.
3. **Security groups**: RDS's security group should only allow port 5432 from the EC2 instance's security
   group (not `0.0.0.0/0`). The EC2 instance's security group should allow 22 (SSH, restricted to your IP)
   and 8501 (Streamlit UI); keep 8000 (API) closed unless you need external API access too.
4. **EC2**: launch a `t3.micro`/`t2.micro` instance (Amazon Linux 2023), using [deploy/ec2-user-data.sh](deploy/ec2-user-data.sh)
   as its user-data to install Docker/Compose and clone the repo on first boot (edit the placeholder GitHub
   URL in that script first, or just `git clone`/`scp` the repo manually after launch).
5. **Configure**: on the instance, copy `.env.example` to `.env`, set `POSTGRES_URL` to the RDS endpoint
   (append `?sslmode=require`), and switch `EMBEDDING_PROVIDER` to `openai` or `google` — the default
   `sentence_transformers` provider loads a local PyTorch model that doesn't comfortably fit a `t3.micro`'s
   1 GiB of RAM. Switching providers means re-ingesting from scratch (fresh DB, so this is a non-issue
   on a first deploy).
6. **Run it**: `docker compose -f docker-compose.prod.yml up -d --build`, then
   `docker compose -f docker-compose.prod.yml exec api python3 -m app.db.bootstrap` and
   `curl -X POST http://localhost:8000/ingestion/all` to populate the database.
7. Visit `http://<ec2-public-ip>:8501` for the UI.

This was deliberately kept to the simplest architecture that fits free-tier instance sizes (single EC2 box,
no ECS/Fargate — Fargate isn't part of the standard Free Tier) rather than a "proper" multi-AZ/autoscaled
setup, consistent with project-plan.md treating AWS as a packaging step, not core architecture.

