# Ingestion

How files under `data/raw/` become searchable rows in PostgreSQL. See [architecture.md](architecture.md) for the overall data flow, project structure, and database schema; this file only covers the ingestion stage itself.

## What it does, in order

For every entry in `config/sources.yaml`:

1. find the file and compute a SHA-256 hash of its content
2. if that hash is already in the database, stop — the file was ingested before
3. otherwise, parse the file into pages, split each page into overlapping chunks, attach metadata to each chunk, embed the chunks, and save the company (if new), the document, and all chunks in one transaction

This makes re-running ingestion safe: unchanged files are skipped, nothing is duplicated, and a failure partway through only affects the one file being processed — everything ingested before it stays saved.

## Step 1 — Describe the sources

Nothing is guessed from a filename. Two YAML files are the source of truth:

- **`config/companies.yaml`** — one entry per company (`id`, `name`, `country`, `category`, `ownership`, `ticker`). The `companies` table is filled from here.
- **`config/sources.yaml`** — one entry per file to ingest (`company_id`, `file_name`, `document_type`, `year`, `title`, `source_url`).

Every `company_id` in `sources.yaml` must exist in `companies.yaml`, and every `file_name` must exist under `data/raw/<company_id>/`. If either is wrong, ingestion stops immediately with a clear error (`KeyError` or `FileNotFoundError`) rather than silently guessing.

## Step 2 — Parse PDF and HTML into pages

Handled by `app/ingestion/parser.py`. The parser looks at the file extension:

- **PDF** → one page of text per PDF page, using `pypdf`. Keeping page numbers is what makes citations like "Annual Report 2025, p. 42" possible later.
- **HTML** → tags are stripped and the visible text is kept as a single page. Text inside `<script>` and `<style>` is dropped, since it's code, not content.

Both paths strip NUL characters, which Postgres text columns cannot store and which a few of the source files contain.

**Known gap: pictures and tables aren't handled.** Neither parser extracts images or does OCR, so image-only pages (scanned slides, charts rendered as pictures) silently produce an empty page and zero chunks — no error, nothing logged. Tables fare no better: `pypdf` has no concept of rows/columns, so table and chart text comes out in PDF content-stream order, which often isn't the same as visual reading order (numbers and labels interleaved); the HTML parser keeps `<table>` cell text but flattens it the same way, losing row/column structure.

Not built yet, but here's the plan for each if it turns out to matter:

- **OCR fallback for empty pages.** In `extract_pages()`, after `text = page.extract_text() or ""`, fall back to OCR only when the page looks empty (e.g. `len(text.strip()) < 20`), so the cost only hits the minority of pages that need it:
  ```python
  if len(text.strip()) < 20:
      image = convert_from_path(str(file_path), first_page=page_number, last_page=page_number)[0]
      text = pytesseract.image_to_string(image)
  ```
  Needs `pdf2image` + the `poppler` binary to rasterize the page, and `pytesseract` + the `tesseract` binary to read it — system dependencies, not pure `pip install`. This fixes pages that currently produce zero chunks; it does not fix jumbled table/chart ordering, since OCR reads whatever layout it's given just as linearly as `pypdf` does.

- **Table-aware extraction.** Swap in `pdfplumber` for the pages (or table regions) that need it, since it can detect ruled/bordered tables and return them as actual rows instead of a flat text blob:
  ```python
  with pdfplumber.open(file_path) as pdf:
      tables = pdf.pages[page_number - 1].extract_tables()
  table_text = "\n\n".join(
      "\n".join(" | ".join(cell or "" for cell in row) for row in table)
      for table in tables
  )
  ```
  Appending `table_text` to the page's plain text keeps each row's cells grouped together instead of scattered across the chunk. This works well for bordered financial-statement tables (common in annual reports) but much less reliably for investor-deck slides, where "tables" are often freeform shapes and chart labels rather than real PDF table objects — so it would help one document type far more than the other.

Fixing this means picking up the dependencies above and accepting slower ingestion for the affected pages — real additions, not yet justified unless it's actually causing wrong answers.

Imperfect parsing is accepted for now — if a handful of PDFs turn out to be unusually hard to parse, fix those specific files rather than building a general-purpose layout engine.

## Step 3 — Split pages into chunks

Handled by `app/ingestion/chunker.py`. Each page is chunked on its own, never the whole document at once, so every chunk keeps the page number it came from.

The approach is a simple sliding window over the text:

- cut at roughly 500 characters, always at a word boundary (never mid-word)
- start the next chunk about 80 characters before the previous one ended, so a sentence near a cut isn't lost from both sides

**Why 500 characters, not more:** the chunk size is bounded by the embedding model's max sequence length, not chosen for readability. The default local model (`all-MiniLM-L6-v2`) silently truncates anything past 256 tokens — text beyond that limit is still stored in `chunks.content`, but it never reaches the embedding, so it can't be found by search. Measured on this corpus, 500 characters keeps every chunk under that limit, including the densest content (investor-presentation slides packed with numbers and currency symbols, which tokenize far less efficiently than narrative prose — as low as ~2 characters per token, versus ~4-5 for plain text). A larger character limit (900 was the original default) looked fine for annual-report prose but silently truncated the embedding of roughly a quarter of all chunks once dense slide content was included. If you switch to a provider with a larger context window (OpenAI, Google), this number can grow — it's tied to the model in `app/services/embeddings.py`, not to the ingestion pipeline.

This is deliberately simple otherwise. Section- or semantic-aware chunking can be added later if plain fixed-size chunking turns out to be a problem.

## Step 4 — Attach metadata to each chunk

Handled by `app/ingestion/metadata.py`. Every chunk gets the same metadata dictionary, built from the manifest and company data — `company_id`, `company_name`, `document_type`, `year`, `page`, `source_file`, `source_url`, `title`. This is what lets retrieval filter "show me only ATOSS chunks" or "only annual reports," and some of these values are intentionally duplicated into chunk columns too — retrieval should be convenient, not normalized for its own sake.

## Step 5 — Generate embeddings

Handled by `app/services/embeddings.py`, used as a dependency rather than hard-coded into the pipeline — swapping providers never requires touching ingestion code.

- the provider is chosen with the `EMBEDDING_PROVIDER` environment variable: `sentence_transformers` (default, local, no API key), `openai`, or `google`
- all chunk texts for one document are embedded together in batches of 64, not one call per chunk
- provider libraries are only imported when that provider is selected, so you don't need `openai` or `google-generativeai` installed to use the local model

## Step 6 — Save to PostgreSQL

Handled by `app/db/repository.py`. One transaction per document: insert the company if it's new, insert the document row, insert all its chunk rows (with their embeddings and metadata). Either all of it is saved or none of it is — there's no partially-ingested document.

## Step 7 — Expose it through FastAPI

`app/api/ingestion.py` wraps the pipeline for HTTP use:

| Endpoint | What it does |
| --- | --- |
| `POST /ingestion/all` | ingest every source in `config/sources.yaml`; returns counts of ingested vs. skipped files |
| `GET /ingestion/status` | returns the number of documents currently stored |
| `POST /ingestion/upload` | multipart: `files` (one or more) + `metadata` (JSON list, one object per file, same order); saves the files and registers them in `sources.yaml` without ingesting them |
| `GET /companies` | lists the companies in `config/companies.yaml` |
| `POST /companies` | appends a new company to `config/companies.yaml` (409 if the id exists) |

`app/main.py` creates the database tables on startup (safe to call repeatedly, never deletes data).

## Adding your own data

The UI's **Data** tab covers the whole flow, in three steps:

1. **Add a company** (optional) — `POST /companies`. Every field except `ticker` is required; `id`, `category` and the screening tags must be lowercase `a-z0-9_`.
2. **Upload documents** — pick several PDF/HTML files at once, then fill in `company_id`, `document_type`, `year`, `title` and `source_url` for every file. `POST /ingestion/upload` (`app/ingestion/uploads.py`) validates the whole batch before writing anything:
   - the company exists in `companies.yaml`
   - the file type is `.pdf`/`.html`/`.htm`, a PDF starts with `%PDF`, max 50 MB per file
   - the file name (directories stripped, other characters than `A-Za-z0-9._-` replaced by `_`) isn't already used for that company

   Files land in `data/raw/<company_id>/`, entries are appended to `sources.yaml`.
3. **Run ingestion** — calls `POST /ingestion/all`. Already ingested files are skipped by hash, so only the new uploads are embedded. Saved screening results for that company are invalidated automatically (the corpus fingerprint changes).

Both manifests are appended as text rather than re-dumped (`_append_list_items()` in `manifest.py`), so their comments and formatting survive; if the re-parsed file doesn't contain the new entries, the original is restored. In Docker, `config/` and `data/raw/` are bind-mounted into the api container so uploads survive a rebuild.

## Running it

Assumes the shared setup from [architecture.md](architecture.md) (`.env` exported, Postgres running, tables created).

**Smoke test** — ingest everything and print one summary line per file:

```bash
python3 - <<'PY'
import asyncio
from app.ingestion.manifest import load_sources
from app.ingestion.pipeline import ingest_all
from app.services.embeddings import EmbeddingService

async def main():
    results = await ingest_all(load_sources(), EmbeddingService())
    for item in results:
        print(item['company_id'], item['file_name'], item['pages'], item['chunks'], item['ingested'])

asyncio.run(main())
PY
```

Run it twice: the first run shows `ingested=True` for every file; the second shows `False` with 0 pages/chunks for all of them, proving the hash check works.

**Through the API:**

```bash
uvicorn app.main:app --reload
curl -X POST http://localhost:8000/ingestion/all
curl http://localhost:8000/ingestion/status
```

**Quality check** — inspect what actually landed in the database:

```bash
# documents and chunks per company
docker compose exec postgres psql -U postgres -d pe_screening -c "SELECT co.name, count(DISTINCT d.id) AS documents, count(k.id) AS chunks FROM companies co JOIN documents d ON d.company_id = co.id JOIN chunks k ON k.document_id = d.id GROUP BY co.name ORDER BY co.name;"

# a few chunks, to eyeball whether the text is clean
docker compose exec postgres psql -U postgres -d pe_screening -c "SELECT page_number, chunk_index, left(content, 200) FROM chunks WHERE company_id = 'atoss' ORDER BY page_number LIMIT 5;"

# embedding size actually stored
docker compose exec postgres psql -U postgres -d pe_screening -c "SELECT vector_dims(embedding) AS dimensions, count(*) FROM chunks GROUP BY 1;"
```

Ingestion bugs are easy to miss once an LLM sits on top of the corpus, since it can make broken data look superficially fine — these queries are how you catch them early.

## Troubleshooting

| Symptom | Cause |
| --- | --- |
| `FileNotFoundError` | `file_name` in `sources.yaml` doesn't match a file under `data/raw/<company_id>/` |
| `KeyError` for a company | `company_id` in `sources.yaml` is missing from `companies.yaml` |
| Embedding call fails | provider API key missing, or the provider library isn't installed |
| Every file shows `ingested=False` on a fresh database | you forgot to run `python3 -m app.db.bootstrap`, or you're pointing at a different `POSTGRES_URL` than you think |
| Schema looks stale after a model change | run `python3 -m app.db.bootstrap --reset`, then ingest again |
