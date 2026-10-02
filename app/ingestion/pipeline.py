from __future__ import annotations

from app.db.repository import document_hash_exists, persist_document_and_chunks
from app.ingestion.chunker import chunk_pages
from app.ingestion.loader import compute_file_hash, resolve_source_path
from app.ingestion.manifest import SourceDocument, get_company
from app.ingestion.metadata import enrich_chunk_metadata
from app.ingestion.parser import extract_pages


async def ingest_document(source: SourceDocument, embedding_service) -> dict:
    """Ingest one source file. Skips it when the same file content is already stored."""
    file_path = resolve_source_path(source)
    file_hash = compute_file_hash(file_path)
    summary = {
        "company_id": source.company_id,
        "file_name": source.file_name,
        "pages": 0,
        "chunks": 0,
        "ingested": False,
    }

    if document_hash_exists(file_hash):
        return summary

    company = get_company(source.company_id)
    pages = extract_pages(file_path)
    chunks = chunk_pages(pages)
    for chunk in chunks:
        enrich_chunk_metadata(chunk, source, company["name"])

    embeddings = await embedding_service.embed_documents([chunk.content for chunk in chunks])
    for chunk, embedding in zip(chunks, embeddings):
        chunk.embedding = embedding

    persist_document_and_chunks(source, company, file_hash, chunks)

    summary.update(pages=len(pages), chunks=len(chunks), ingested=True)
    return summary


async def ingest_all(sources: list[SourceDocument], embedding_service) -> list[dict]:
    """Ingest every source in the manifest, one after the other."""
    return [await ingest_document(source, embedding_service) for source in sources]
