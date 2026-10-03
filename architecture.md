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

Live deployment layout: one EC2 instance (`t3.micro`) running the `api` and `ui` containers via
`docker-compose.prod.yml`, plus a managed RDS PostgreSQL instance (`db.t3.micro`, engine 16.x) instead of
the local `postgres` container. Managed entirely by Terraform in [terraform/](terraform/) — `main.tf`
(provider), `network.tf` (default VPC, security groups, DB subnet group), `rds.tf`, `ec2.tf` +
`templates/user-data.sh.tftpl` (first-boot script: Docker, Compose, buildx, a swap file, and cloning the
repo), `budget.tf` (cost alert), `outputs.tf` (public IP, RDS endpoint, UI URL). `docker-compose.prod.yml`
differs from `docker-compose.yml` only by dropping the local `postgres` service — `POSTGRES_URL` in `.env`
points at the RDS endpoint instead.

> New AWS accounts get **$100–200 in credits valid for 6 months**, not the older "12 months of free
> EC2/RDS hours" — check the Billing console's Free Tier page for what applies to your account. A
> `t3.micro` + `db.t3.micro` running 24/7 cost only a few dollars a month even outside the credit; set an
> AWS Budget alert (`budget.tf` does this automatically) as a safety net regardless.
>
> Accounts created via AWS's simplified "Sign up for AWS (new)" flow (AWS Builder ID / "projects") run
> under an AWS-managed Service Control Policy that blocks raw EC2/RDS API calls Terraform needs (e.g.
> `ec2:DescribeVpcs`) — if `terraform plan` fails with "explicit deny in a service control policy", you
> need to **activate advanced features** first (irreversible; via `settings.aws.com` → Projects → Actions
> → Explore advanced features). This converts the account into a self-administered AWS Organization. The
> default post-activation SCP (`AdvancedModeRegionRestrictionSecurityControlPolicy`) only allows one
> region until you edit it (AWS Organizations console, logged in **as the management account**, not the
> project account) to add the region you actually use (check `aws configure get region`).

Steps, once `aws configure` works and (if applicable) the account restrictions above are resolved:

1. `cd terraform && cp terraform.tfvars.example terraform.tfvars` and fill in `db_password` (generate one,
   don't reuse anything real), `key_pair_name` (create via `aws ec2 create-key-pair`), `allowed_ssh_cidr`
   (your IP, `/32` — never `0.0.0.0/0`), `git_repo_url` (a public repo the instance can clone anonymously),
   `budget_alert_email`.
2. `terraform init && terraform plan -out=plan.out && terraform apply plan.out`. Creates the VPC security
   groups, RDS instance (~5-10 min), EC2 instance, and budget alert.
3. SSH in once the instance's user-data has finished (`git clone` done): build an `.env` on the instance
   (copy the local one, point `POSTGRES_URL` at the RDS endpoint from `terraform output rds_endpoint` with
   `?sslmode=require` appended), then `docker compose -f docker-compose.prod.yml up -d --build`,
   `docker compose -f docker-compose.prod.yml exec api python3 -m app.db.bootstrap`, and
   `curl -X POST http://localhost:8000/ingestion/all`.
4. Visit `http://<ec2_public_ip>:8501` (from `terraform output streamlit_url`) for the UI.

Two things worth knowing if you redo this:
- **Embedding provider for bulk ingestion**: Google's free-tier `embedContent` quota is capped at 1000
  requests/day, and `ingest_document` only persists a document after *all* its chunks embed — a failed
  bulk ingest re-embeds from scratch next time, so repeated retries can exhaust the daily quota with zero
  progress saved. For a one-time ingest of a non-trivial corpus, it's more reliable to temporarily resize
  the EC2 instance up (`ec2_instance_type = "t3.medium"` in `terraform.tfvars`, `terraform apply`) and use
  the default `sentence_transformers` local model instead (no external quota), then resize back down to
  `t3.micro` afterward (`terraform apply` again — this modifies the instance in place, same instance ID,
  but the public IP changes on every stop/start since there's no Elastic IP). Whichever provider ends up
  used for ingestion must stay fixed afterward — query embeddings have to match the space the stored
  document vectors were created in.
- **`t3.micro` RAM is tight** (≈916 MiB usable) once `api` has the embedding model loaded alongside `ui` —
  verified working (a real `/ask` request succeeds), but with only tens of MiB free. The user-data script
  adds a 1 GB swap file as an OOM safety margin; consider `t3.small` if you see containers getting killed
  under real concurrent use.

This was deliberately kept to the simplest architecture that fits free-tier instance sizes (single EC2 box,
no ECS/Fargate — Fargate isn't part of the standard Free Tier) rather than a "proper" multi-AZ/autoscaled
setup, consistent with project-plan.md treating AWS as a packaging step, not core architecture.


