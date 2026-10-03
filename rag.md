# RAG: concepts and lessons from this project

This is an overview of Retrieval-Augmented Generation (RAG): what it is, how each stage works, and which design
choices matter in practice. Every section pairs the general idea with what this project actually built, measured,
or got wrong along the way. For implementation detail, see [architecture.md](architecture.md),
[ingestion.md](ingestion.md), [retrieval.md](retrieval.md), and [use-cases.md](use-cases.md).

## 1. What RAG is, and why it's used

A large language model only knows what was in its training data, can't cite where a claim came from, and will
produce a fluent answer even when it has no real basis for one. RAG addresses all three by splitting the work in
two:

1. **Retrieval** — find the passages in a known document collection that are relevant to the question.
2. **Generation** — give the LLM the question *plus those passages*, and instruct it to answer only from them.

The model's job shifts from "remember the answer" to "read these sources and summarize what they say". That makes
answers:

- **grounded** — based on specific documents, not on the model's memory
- **citable** — every answer can point to the exact page it came from
- **current** — updating knowledge means ingesting new documents, not retraining a model
- **honest about gaps** — when retrieval finds nothing, the system can say so instead of guessing

### RAG vs. the alternatives

| Approach | What it does | Why it wasn't used here |
| --- | --- | --- |
| Plain LLM prompt | Ask the model directly | No citations, no access to the data room, hallucination risk on company-specific facts |
| Fine-tuning | Train the model on the documents | Expensive, slow to update, still can't cite sources, and teaches style better than facts |
| Long-context stuffing | Paste every document into the prompt | ~60 documents (annual reports of 100+ pages) far exceed any free-tier context window and token budget; cost scales with corpus size on every request |
| **RAG** | Search first, then prompt with only the relevant passages | Cost per request is bounded by `top_k`, citations come for free, new documents are one ingestion run away |

In a due-diligence setting, citations aren't optional: an analyst has to be able to check every claim against the
annual report it supposedly came from. That alone makes RAG the natural fit for this project.

## 2. The two halves of a RAG system

A RAG system has an **offline** half that prepares the documents once, and an **online** half that runs on every
question.

```
OFFLINE (indexing, once per document)              ONLINE (per question)

 raw files (PDF, HTML)                               question
      │                                                 │
      ▼                                                 ▼
 parse  → pages of text                            embed the question  (same model as indexing!)
      │                                                 │
      ▼                                                 ▼
 chunk  → small, overlapping passages              vector search → top_k closest chunks
      │                                                 │           (+ metadata filters, distance cutoff)
      ▼                                                 ▼
 enrich → attach metadata (company, page, ...)     build prompt: instructions + numbered sources + question
      │                                                 │
      ▼                                                 ▼
 embed  → one vector per chunk                     LLM generates an answer citing [1], [2], ...
      │                                                 │
      ▼                                                 ▼
 store  → vector database (pgvector)  ─────────►   answer + the list of chunks it was based on
```

Quality is decided mostly in the offline half. An LLM can only reason over what retrieval hands it, and retrieval
can only find what ingestion stored correctly. In this project, whenever an answer looked wrong, the cause was
almost always upstream: a chunk that was truncated, a page that parsed as empty, or a query phrased unlike the
text it should have matched.

## 3. Ingestion: turning documents into searchable chunks

### Parsing

Parsing extracts plain text from source files. It sounds trivial and is where a lot of silent damage happens.

- **Keep structure that citations need.** PDFs are parsed page by page (`pypdf`), so every chunk knows its page
  number. Without that, "Annual Report 2025, p. 42" is impossible later.
- **Strip what isn't content.** HTML `<script>`/`<style>` text is dropped. NUL characters are stripped because
  Postgres text columns can't store them, and a few of the source files contain them.
- **Know what the parser can't see.** Image-only pages (scanned slides, charts rendered as pictures) produce an
  empty page and zero chunks — no error, nothing logged. Tables come out in PDF content-stream order, which often
  interleaves numbers and labels. Both are documented gaps (OCR fallback, `pdfplumber` table extraction) rather
  than fixed, because they haven't yet caused wrong answers. The general lesson: a parser's failures are
  invisible downstream unless the stored text is inspected directly.

### Chunking

Documents are too long to embed or prompt with whole, so they are split into **chunks**: the unit that gets
embedded, searched, and shown to the LLM.

The trade-off:

| Smaller chunks | Larger chunks |
| --- | --- |
| More precise embeddings (one idea per vector) | More context per hit, fewer fragmented sentences |
| More chunks to search and store | Embedding becomes a blurry average of several topics |
| Risk of cutting a fact away from its context | Risk of exceeding the embedding model's input limit |

What this project does (`app/ingestion/chunker.py`):

- **Fixed-size sliding window**, ~500 characters, cut at word boundaries, with ~80 characters of **overlap** so a
  sentence near a cut isn't lost from both neighbours.
- **Chunk per page, never across pages**, so each chunk has exactly one page number to cite.

**The most important chunking lesson: chunk size is bounded by the embedding model, not by readability.** The
default local model (`all-MiniLM-L6-v2`) silently truncates input beyond 256 tokens. Text past that limit is still
stored in the database and still shown to the LLM, but it never reaches the embedding — so it can never be
*found*. The original 900-character chunks looked fine for annual-report prose (~4–5 characters per token) but
investor-presentation slides packed with numbers and currency symbols tokenize at ~2 characters per token. Once
those were in the corpus, roughly a quarter of all chunks had truncated embeddings. Measuring token counts on the
real corpus is what led to 500 characters. Nothing errors when this goes wrong; retrieval just quietly gets worse.

More sophisticated strategies exist — section-aware chunking (split on headings), semantic chunking (split where
the topic shifts), or parent/child chunking (see section 12) — but fixed-size chunking was good enough once the size
matched the model.

### Metadata

Every chunk carries a metadata dictionary: `company_id`, `company_name`, `document_type`, `year`, `page`,
`source_file`, `source_url`, `title`. Metadata does two jobs:

1. **Filtering** — "only ATOSS chunks" is a SQL `WHERE`, not a hope that the embedding happens to favour ATOSS.
   Pure semantic search has no concept of which company a passage belongs to; two companies' "recurring revenue
   grew 12%" sentences look nearly identical as vectors.
2. **Citation** — the document title and page shown next to an answer come straight from the metadata.

Metadata comes from two YAML manifests (`config/companies.yaml`, `config/sources.yaml`), never guessed from file
names. A wrong `company_id` fails ingestion loudly instead of quietly misattributing a document.

### Idempotency

Re-running ingestion must be safe. Each file's SHA-256 content hash is stored with a unique constraint, so an
unchanged file is skipped and can never be duplicated. Each document is saved in one transaction (document row
plus all its chunks), so a failure leaves no half-ingested document behind.

The flip side, learned the hard way: all-or-nothing persistence means a document whose embedding fails on its last
chunk has to be re-embedded from scratch. With Google's free-tier embedding quota (1,000 requests/day), repeated
retries of a bulk ingest burned the entire daily quota with zero documents saved. The fix was switching to the
local model (no quota) on a temporarily larger EC2 instance. Rate-limited embedding APIs and all-or-nothing writes
don't mix well for bulk ingestion.

## 4. Embeddings: turning text into searchable meaning

An **embedding** is a fixed-length vector of numbers that represents the meaning of a piece of text. Texts with
similar meaning end up close together in that vector space, even when they share no words: *"subscription
revenue"* and *"recurring SaaS income"* land near each other; *"chocolate cake recipe"* lands far away.

### Measuring closeness

The standard measure is **cosine similarity** (the angle between two vectors), or its complement **cosine
distance** $= 1 - \cos\theta$:

$$
\text{cosine distance}(a, b) = 1 - \frac{a \cdot b}{\lVert a \rVert \, \lVert b \rVert}
$$

0 means identical direction (same meaning), 1 means unrelated (orthogonal). The local model's embeddings are
normalized to unit length, for which cosine distance is the natural fit. pgvector exposes it as the `<=>`
operator.

### Rules learned about embeddings

- **Query and documents must use the same model.** Distances between vectors from different models are
  meaningless — they live in different spaces, often with different dimensions (384 for MiniLM, 1536 for
  OpenAI's `text-embedding-3-small`, others for Google). `retrieve()` therefore takes the same `EmbeddingService`
  used for ingestion instead of building its own.
- **Switching providers means re-embedding the whole corpus.** There's no conversion between vector spaces. The
  schema deliberately leaves the vector column's dimension unspecified so any provider fits, but one database must
  only ever contain one provider's vectors (`python3 -m app.db.bootstrap --reset`, then re-ingest).
- **Distance scales differ per model.** Any threshold tuned on one model (see the distance cutoff below) has to be
  re-measured after a switch.
- **Every model has an input limit** — and exceeding it truncates silently (see chunking above).
- **Some APIs distinguish documents from queries.** Google's embedding API takes a `task_type`
  (`retrieval_document` vs. `retrieval_query`), because the two are asymmetric: a short question and the long
  passage that answers it are different kinds of text.
- **Batch document embeddings, cache query embeddings.** Chunks are embedded 64 per call. Query embeddings are kept
  in an in-memory LRU cache, because screening asks the same ~20 criterion queries for every company.
- **Local models have operational quirks.** Concurrent `SentenceTransformer.encode()` calls from several threads
  crash the process on macOS, so local-model calls run behind a lock. And the model has to fit in RAM: on a
  `t3.micro` (≈916 MiB usable) the API with MiniLM loaded plus the UI leaves only tens of MiB free.

### Local vs. hosted embedding models

| | Local (`sentence_transformers`, default) | Hosted (OpenAI, Google) |
| --- | --- | --- |
| Cost / quota | Free, no quota | Per-token cost or tight free-tier quotas |
| Quality | Good enough for this corpus | Generally stronger, longer input limits |
| Input limit | 256 tokens (MiniLM) | Thousands of tokens |
| Ops | Needs RAM/CPU, model download, thread-safety care | Needs API key, network, retry logic |

For this project the local model won: free, no quota surprises, and the smaller input limit was solved by
chunk size.

## 5. The vector store

The vectors need to live somewhere that can answer "which stored vectors are closest to this one?" quickly.
Options range from dedicated vector databases (Pinecone, Weaviate, Qdrant, Chroma) to vector extensions on a
regular database.

This project uses **PostgreSQL + pgvector**:

- **One system for everything.** Companies, documents, chunk text, metadata, and vectors sit in the same
  database. A company filter is an ordinary SQL `WHERE` applied before the distance is computed, and results
  join directly to document titles for citations.
- **Postgres is the source of truth.** Anything downstream (answers, screening reports, comparisons) reads from
  the database, never from in-memory objects of the ingestion run that produced them.
- **Exact search, no approximate index.** Queries scan the (company-filtered) chunks and sort by exact distance.
  At this corpus size that's fast enough. Larger corpora need an approximate-nearest-neighbour (ANN) index —
  pgvector offers HNSW and IVFFlat — which trades a little recall for much faster search. Note that pgvector's
  indexes need a fixed vector dimension, which the provider-agnostic `Vector()` column here deliberately doesn't
  declare; adding an index means pinning the dimension to one model.

## 6. Retrieval: finding the right evidence

Retrieval is the stage that most determines answer quality. The LLM can't cite a passage it was never shown.

### Top-k

Retrieval returns the `top_k` closest chunks. UC1 (Ask) uses 5, screening uses 3 per criterion. Too few, and the
relevant passage may be missed; too many, and the prompt fills with marginally related text, costs more tokens,
and gives the model more room to latch onto the wrong passage.

### The distance cutoff: making "I don't know" possible

Nearest-neighbour search **always** returns `top_k` results, no matter how unrelated. Ask about a chocolate cake
recipe and it still returns the five "closest" annual-report chunks. Without a cutoff, the "no evidence → don't
call the LLM" rule could never fire, and the LLM would always be shown *something*.

`RETRIEVAL_MAX_DISTANCE` drops chunks above a distance threshold, in the SQL `WHERE`. The value was measured on the
real corpus, not guessed:

| Query | Best match per company (cosine distance, MiniLM) |
| --- | --- |
| Screening criteria (question + retrieval queries) | 0.19 – 0.62 |
| Off-topic: *"football world cup results"* | 0.61 – 0.77 |
| Off-topic: *"chocolate cake recipe"* | 0.81 – 0.88 |

0.70 keeps every legitimate best match with some margin, while dropping clearly unrelated text (website language
menus sat at 0.76–0.82). Two caveats: the ranges overlap, so the cutoff is a coarse filter, not a relevance
judgement; and it's model-specific, so it has to be re-measured after an embedding model change.

### Query–document mismatch, and multi-query retrieval

Questions don't look like the sentences that answer them. *"What evidence is there of organic growth?"* is
phrased nothing like *"revenue grew 9% at constant currency"*, so their embeddings aren't as close as they should
be. This is one of the most common reasons RAG retrieval underperforms.

The fix here: each screening criterion lists `retrieval_queries` in `config/screening_config.yaml`, written the
way a report would state the fact (*"EBITDA margin"*, *"free cash flow"*, *"largest customers' share of
revenue"*). `retrieve_multi()` searches the question **and** every phrasing, then merges the results — each chunk
once, at its best distance, cut to `top_k`. The LLM is still asked the original question.

Measured average top-1 distance across 8 companies:

| Criterion | Question only | Question + retrieval queries |
| --- | --- | --- |
| `organic_growth` | 0.65 | 0.43 |
| `geographic_concentration` | 0.66 | 0.47 |
| `margin` | 0.49 | 0.33 |

Hand-written query variants work because the criteria are fixed and known in advance. For free-form questions,
the same idea is usually automated: an LLM rewrites the query into several variants (multi-query), or writes a
hypothetical answer and embeds that instead (HyDE).

### Metadata filtering

Filtering by company happens in SQL, before ranking. It's what makes per-company screening and side-by-side
comparison reliable: comparing two companies is two independent, equally filtered retrievals, not one mixed
search where one company's documents might crowd out the other's.

## 7. Generation: turning evidence into an answer

### Prompt design

Both prompts (`app/workflows/ask.py`, `app/workflows/screening.py`) follow the same pattern:

1. **Role and constraint:** *"using only the numbered sources below. Do not use any outside knowledge."*
2. **Numbered sources:** each chunk formatted as `[n] Title (file, p. X): text`.
3. **Citation instruction:** cite inline as `[1]`, `[2]`.
4. **Explicit permission to say "not enough information"** instead of guessing.

The fourth point matters more than it looks. LLMs are trained to be helpful and will fill gaps unless told that
declining is an acceptable answer.

### Grounding rules this project enforces in code, not just in the prompt

- **No evidence → no LLM call.** If retrieval returns nothing (no documents, or nothing within the distance
  cutoff), the workflow returns a fixed "insufficient evidence" result without prompting the model at all. A model
  that's never asked can't hallucinate.
- **Citations are the retrieved chunks, not parsed from the LLM's text.** The `sources` returned are always
  exactly the chunks that went into the prompt. The model's `[n]` markers point into that list, but nothing
  depends on parsing them reliably.
- **Absence of evidence isn't evidence of absence.** For risk criteria, documents not mentioning customer
  concentration yields `insufficient_evidence`, never "no risk".

### Classification instead of scores

Asking an LLM to "score recurring revenue from 1–10" invites invented precision: the number has no defined
meaning and varies between runs. The screening framework instead asks the model to **classify the evidence**:
`strong_evidence`, `moderate_evidence`, `weak_evidence`, or `insufficient_evidence`, with a 2–4 sentence cited
rationale. The question becomes "how well do these sources support this?" — something the model can actually judge
from the text in front of it.

**Polarity** turned out to matter: "strong evidence of customer concentration" is bad news, not good. Risk
criteria get an extra prompt paragraph defining `strong_evidence` as "the risk is clearly present", and the UI
labels them as risks (🔴 / 🟠 / 🟡) instead of on the green scale.

### Structured output

Screening needs machine-readable output. The prompt asks for a JSON object with exactly two keys, and
`_parse_assessment()` parses it tolerantly:

- extracts the first `{...}` block, so a code fence or a sentence of preamble doesn't break parsing
- falls back to `insufficient_evidence` if the JSON is invalid or the assessment isn't one of the four allowed
  values

A malformed model reply degrades to "no conclusive evidence", never to a crash. Provider-native structured-output
APIs (JSON schema modes, function calling) are stricter alternatives, but differ per provider; prompted JSON plus
defensive parsing works across OpenAI, Google, and Groq with one code path.

## 8. Orchestration: from one question to a framework

### Decompose instead of asking one giant question

The most important design decision in the project isn't a RAG technique, it's **decomposition**. "Analyze
Nemetschek as a PE investment" as one RAG query would retrieve five vaguely relevant chunks and produce a generic
essay. Instead, the screening framework (`config/screening_config.yaml`) breaks the question into ~20 targeted
criteria across five dimensions, and each criterion gets **its own retrieval and its own assessment**:

```
                        company documents
                               │
         ┌─────────────────────┼──────────────────────┐
         ▼                     ▼                      ▼
 recurring revenue     acquisition history     customer concentration   ... ×20
   retrieve(3)            retrieve(3)              retrieve(3)
   assess                 assess                   assess
         └─────────────────────┼──────────────────────┘
                               ▼
                   report grouped by dimension
```

Each criterion gets its best-matching evidence instead of competing for the same five slots. Each assessment is
small, focused, and independently checkable. A gap in one criterion shows up as one `insufficient_evidence`
row instead of silently thinning out an essay.

The four use cases are all built from the same two building blocks:

| Use case | Built from |
| --- | --- |
| UC1 Ask | `retrieve` → `generate` (free-text answer) |
| UC2 Screening | `retrieve_multi` → `assess`, once per criterion |
| UC3 Compare | UC2 for two companies, merged by criterion id |
| UC4 Universe | one criterion's `retrieve_multi` → `assess` for every (company, criterion) pair |

### LangGraph

UC1 and UC2 are two-node LangGraph graphs (`retrieve` → `generate`/`assess`) over a typed state dictionary. For
graphs this small, LangGraph is mostly structure: each node is a plain function of state in, partial state out,
which keeps nodes trivially testable. Services (embedding, LLM) are injected into the graph builder rather than
constructed inside it, so tests run the real graph with fake services. LLM calls use the providers' plain SDKs, not
LangChain wrappers — LangGraph only needs a `generate(prompt) -> str` function.

The graph abstraction starts paying off for multi-step flows: conditional branches (e.g. re-retrieve with a
rewritten query when evidence is weak), loops, or agent-style tool use. None of those were needed yet.

## 9. Running RAG for real: cost, limits, caching, failures

Most of the practical lessons in this project came from operating the system, not from building the pipeline.

### LLM providers and rate limits

- **Free tiers are the real constraint.** Groq's free tier allows 8,000 tokens per minute (about 8 screening
  calls) *and* 200,000 tokens per day. A full screening report is ~20 LLM calls; with 4 in flight it failed with
  `429` even with retries. A single semaphore in `ChatService` (`LLM_MAX_CONCURRENCY`, default 1) caps calls in
  flight for the whole process, so workflows don't each need to know about rate limits.
- **Retries cover transient failures only.** SDK retries with backoff (honouring `retry-after`) fix per-minute
  limits and Google's occasional `503 UNAVAILABLE`. They don't fix a daily cap, a wrong model name (`404`), or a
  zero-quota tier (`429` that never clears).
- **Model availability changes fast.** Google model names that worked weeks earlier started returning `404` for new
  keys, and newer Pro models had a zero free-tier quota. Rolling aliases (`models/gemini-flash-latest`) and checking
  `client.models.list()` beat hardcoding a version. The default provider ended up being Groq
  (`openai/gpt-oss-120b`): fast, free, and stable.
- **Provider switches are cheap for the LLM, expensive for embeddings.** Swapping `LLM_PROVIDER` is a config
  change. Swapping `EMBEDDING_PROVIDER` means re-ingesting everything.

### Concurrency

Embedding, database access, and LLM calls are all blocking SDKs, so each runs in a worker thread
(`asyncio.to_thread`). Screening starts all its criteria at once with `asyncio.gather`; retrieval overlaps, and
LLM calls queue behind the semaphore. On a paid tier, raising `LLM_MAX_CONCURRENCY` speeds everything up with no
code change.

### Failure isolation

One criterion hitting the daily token cap used to raise inside `asyncio.gather` and fail the entire report,
discarding ~17 successful assessments. `screen_criterion_safe()` now turns any exception into an
`insufficient_evidence` result flagged `"error": true`, which is shown distinctly in the UI ("⚠️ Failed") and never
cached. In a pipeline of many independent LLM calls, one failure should cost one row, not the whole result.

### Caching generated results

LLM calls are the slow and expensive part. Each (company, criterion) assessment is saved under `data/analysis/`
with a **fingerprint** of everything that could change it: criterion wording and retrieval queries, prompt
template, `top_k` and cutoff, embedding and LLM models, and a hash of the company's documents. A request whose
fingerprint matches reuses the saved result; anything else re-assesses.

Effects: a repeat ATOSS report took 0.14 s instead of ~110 s; screening a company in UC2 makes it free in UC3 and
UC4; demos are stable instead of varying between runs. Because the fingerprint covers every input, there's no
manual invalidation step to forget, and deleting the folder is always safe.

## 10. Evaluating a RAG system

RAG can fail in three distinct places, and each needs its own check:

| Question | Stage | Failure looks like |
| --- | --- | --- |
| Did the right passage get retrieved? | Retrieval | Answer is vague or "insufficient evidence" despite the fact being in the documents |
| Is the answer supported by the retrieved passages? | Generation (groundedness) | Answer contains claims not present in any source — hallucination |
| Does each citation point to the passage that supports the claim? | Generation (citation) | Claim is true but `[2]` points to an unrelated chunk |

Separating them matters: a good answer with a wrong citation and a wrong answer from bad retrieval need entirely
different fixes.

What was actually done in this project:

- **Inspecting stored data directly.** SQL queries on chunk counts per company, sample chunk text, and stored
  vector dimensions — because an LLM on top of a broken corpus can make broken data look superficially fine.
- **Measuring distances** on real queries and off-topic queries to set the cutoff and to verify that
  `retrieval_queries` actually helped (tables in section 6).
- **Deterministic tests without real models.** Tests insert chunks with hand-picked embeddings for fake companies
  and use dummy chat services. They verify the pipeline's logic — filtering, cutoff, merging, no-LLM-on-no-evidence,
  tolerant parsing, caching — fast and without API keys. They don't measure answer *quality*.
- **Manual end-to-end checks** against the real corpus with a live LLM.

Not built yet: the golden-question set (project-plan.md, Phase 7) — ~10 manually verified questions with the
expected source passage, checked automatically for retrieval hit, groundedness, and citation correctness. Without
it, a change to chunking, prompts, or models can't be shown to improve or regress quality other than by eye.
Frameworks like RAGAS automate similar metrics (context recall/precision, faithfulness, answer relevance), often
using an LLM as the judge.

## 11. Debugging bad answers

Work backwards through the pipeline; the cause is usually earlier than it looks.

| Symptom | Likely cause | Where to look |
| --- | --- | --- |
| "Insufficient evidence", but the fact is in the documents | Page parsed as empty (image/scan), chunk embedding truncated, or query phrased unlike the text | Stored chunk text for that page; token length of the chunk; add `retrieval_queries` |
| Retrieved chunks are on-topic but from the wrong company | Missing metadata filter | `company_id` passed to `retrieve()` |
| Retrieved chunks are unrelated | Query/corpus embedded with different models, or cutoff unset | `EMBEDDING_PROVIDER` vs. what the database was built with; `RETRIEVAL_MAX_DISTANCE` |
| Numbers in an answer are garbled | Table text extracted in content-stream order | Raw chunk text from that page |
| Answer makes claims not in the sources | Prompt too permissive, or too many marginal chunks in context | Prompt wording; lower `top_k` |
| Answers stop changing after a document update | Cached result considered still valid | Fingerprint inputs; `refresh: true` |
| Requests fail intermittently | Rate limits or quota | `LLM_MAX_CONCURRENCY`; provider's per-minute and per-day limits |

## 12. Techniques not used (yet), and when they'd help

| Technique | What it does | When it would be worth adding |
| --- | --- | --- |
| Reranking | A cross-encoder re-scores the top ~20–50 vector hits against the query and keeps the best few | When the right chunk is retrieved but ranks below `top_k` |
| Hybrid search | Combines keyword search (BM25 / Postgres full-text) with vector search | Exact terms matter: product names, tickers, specific figures that embeddings blur |
| Query rewriting / HyDE | An LLM generates query variants or a hypothetical answer to embed | Free-form questions (UC1), where hand-written `retrieval_queries` don't exist |
| Small-to-big / parent-document retrieval | Match on a small chunk, return its surrounding page or section to the LLM | When single chunks lack the context needed to judge a fact |
| Section-aware or semantic chunking | Split at headings or topic shifts instead of fixed sizes | When fixed windows regularly split facts from their context |
| OCR and table extraction | Read image-only pages; keep table rows together | When scanned slides or financial tables cause wrong or missing answers |
| ANN index (HNSW) | Approximate nearest-neighbour search | When the corpus grows beyond what an exact scan handles quickly |
| Conversation memory | Carry previous turns into retrieval and prompting | Multi-turn follow-up questions |
| Agentic RAG | The LLM decides when and what to retrieve, iterating until satisfied | Open-ended research questions that don't decompose into a fixed framework |

The guiding rule throughout: add a technique when a measured problem calls for it, not because it exists.
Multi-query retrieval was added because distances showed obvious matches being missed; reranking wasn't, because
that fix closed the gap.

## 13. Key takeaways

1. **Retrieval quality caps answer quality.** Most "LLM problems" were retrieval or ingestion problems.
2. **Chunk size is set by the embedding model's token limit**, and exceeding it fails silently. Measure tokens on
   the densest real content, not on prose.
3. **Same embedding model for queries and documents, always.** Switching means re-ingesting, and re-tuning any
   distance threshold.
4. **Nearest-neighbour search never returns "nothing".** A measured distance cutoff is what makes "insufficient
   evidence" a reachable answer.
5. **Questions don't look like answers.** Searching several document-style phrasings closed much of the gap.
6. **Metadata filters do what embeddings can't**: scoping by company, document type, or year.
7. **Enforce grounding in code, not only in the prompt**: skip the LLM when there's no evidence, and return the
   retrieved chunks as citations rather than parsing them out of generated text.
8. **Decompose big questions into a framework of small ones.** Targeted retrieval per criterion beats one broad
   query.
9. **Ask the LLM to classify evidence, not to invent scores**, and parse its structured output defensively.
10. **Operating RAG is mostly about limits**: rate limits, daily quotas, model deprecations, RAM. Centralize the
    limits, isolate failures, cache generated results with a complete fingerprint.
11. **Evaluate retrieval, groundedness, and citations separately**, and inspect the stored data directly — an LLM
    on top makes broken data look fine.
