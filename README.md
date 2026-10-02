# pe-deal-screening-copilot
Lightweight AI copilot that helps a Private Equity analyst screen a potential acquisition target from a small virtual data room.

## Current implementation

The repository now contains a working ingestion and retrieval scaffold for:

- manifest-driven source loading
- file hashing and idempotent ingestion (already-ingested files are skipped)
- PDF and HTML page extraction
- page-level chunking with overlap
- metadata enrichment for PE use cases
- embeddings stored in PostgreSQL + pgvector
- pgvector cosine-distance retrieval, optionally filtered by company
- a LangGraph `ask` workflow: retrieve evidence, then an LLM answers with inline citations
- a LangGraph `screening` workflow: assesses every criterion in `config/screening_config.yaml` for a company,
  classifying evidence as strong/moderate/weak/insufficient, grouped by dimension
- a `universe` workflow: assesses one criterion across every ingested company, returning companies that meet a
  minimum evidence threshold
- a `compare` workflow: applies the same screening criteria to two companies side by side, optionally filtered
  to one dimension
- FastAPI app entrypoint

## Quick start

1. Install dependencies: `pip install -r requirements.txt`
2. Export the environment: `cp .env.example .env` then `set -a; source .env; set +a`
3. Start Postgres and create the tables: `docker compose up -d && python3 -m app.db.bootstrap`
4. Run the app: `uvicorn app.main:app --reload`
5. Trigger the ingestion pipeline via `POST /ingestion/all`
6. Ask a question via `POST /ask` (needs `LLM_PROVIDER` + an API key set in `.env`)
7. Generate a PE screening report via `POST /screen` (same LLM requirement)
8. Find companies matching one criterion via `POST /screen/universe` (same LLM requirement)
9. Compare two companies via `POST /compare` (same LLM requirement)
10. Validate with: `python3 -m pytest tests -q`

## Documentation

How the system is built, in the order it was built:

- [architecture.md](architecture.md) — data flow, project structure, database schema, shared local setup
- [ingestion.md](ingestion.md) — how files under `data/raw/` become rows in Postgres
- [retrieval.md](retrieval.md) — how a question becomes ranked evidence chunks
- [use-cases.md](use-cases.md) — the four use cases (ask, screen, compare, universe screening), how each is
  implemented (LangGraph), and the API/tests behind each one

Product vision and planning notes, not yet fully implemented:

- [project-plan.md](project-plan.md) — use cases, day-by-day build plan
- [data.md](data.md) — target company universe and source documents
- [framework.md](framework.md) — the PE screening framework (criteria, assessment model)

