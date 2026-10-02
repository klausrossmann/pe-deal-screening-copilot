from __future__ import annotations

from app.ingestion.manifest import SourceDocument
from app.schemas.documents import Chunk


def enrich_chunk_metadata(chunk: Chunk, source: SourceDocument, company_name: str) -> None:
    chunk.metadata = {
        "company_id": source.company_id,
        "company_name": company_name,
        "document_type": source.document_type,
        "year": source.year,
        "page": chunk.page_number,
        "source_file": source.file_name,
        "source_url": source.source_url,
        "title": source.title,
    }
