from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from app.db.models import ChunkRecord, Company, Document
from app.db.session import get_session
from app.ingestion.manifest import SourceDocument
from app.schemas.documents import Chunk


def document_hash_exists(content_hash: str) -> bool:
    with get_session() as session:
        return session.scalar(select(Document.id).where(Document.content_hash == content_hash)) is not None


def count_documents() -> int:
    with get_session() as session:
        return session.scalar(select(func.count()).select_from(Document)) or 0


def list_companies_with_documents() -> list[str]:
    """Distinct company ids with at least one ingested document (the screenable universe for UC4)."""
    with get_session() as session:
        rows = session.execute(select(Document.company_id).distinct().order_by(Document.company_id)).all()
    return [row[0] for row in rows]


def persist_document_and_chunks(
    source: SourceDocument,
    company: dict[str, Any],
    file_hash: str,
    chunks: list[Chunk],
) -> None:
    """Save company (if new), document and chunks in a single transaction."""
    with get_session() as session:
        if session.get(Company, source.company_id) is None:
            session.add(
                Company(
                    id=source.company_id,
                    name=company["name"],
                    country=company.get("country"),
                    category=company.get("category"),
                    ownership=company.get("ownership"),
                    ticker=company.get("ticker"),
                )
            )
            session.flush()

        document_id = str(uuid.uuid4())
        session.add(
            Document(
                id=document_id,
                company_id=source.company_id,
                document_type=source.document_type,
                title=source.title,
                year=source.year,
                source_url=source.source_url,
                file_name=source.file_name,
                content_hash=file_hash,
            )
        )
        session.flush()

        session.add_all(
            ChunkRecord(
                id=str(uuid.uuid4()),
                document_id=document_id,
                company_id=source.company_id,
                chunk_index=chunk.chunk_index,
                page_number=chunk.page_number,
                content=chunk.content,
                embedding=chunk.embedding,
                metadata_=chunk.metadata,
            )
            for chunk in chunks
        )
        session.commit()
