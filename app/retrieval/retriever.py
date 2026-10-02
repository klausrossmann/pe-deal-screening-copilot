from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db.models import ChunkRecord, Document
from app.db.session import get_session
from app.services.embeddings import EmbeddingService


async def retrieve(
    query: str,
    embedding_service: EmbeddingService,
    company_id: str | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Return the top_k chunks closest to the query, using pgvector cosine distance."""
    query_embedding = await embedding_service.embed_query(query)
    distance = ChunkRecord.embedding.cosine_distance(query_embedding).label("distance")

    stmt = (
        select(
            ChunkRecord.company_id,
            ChunkRecord.page_number,
            ChunkRecord.content,
            ChunkRecord.metadata_,
            Document.title,
            Document.file_name,
            distance,
        )
        .join(Document, Document.id == ChunkRecord.document_id)
        .order_by(distance)
        .limit(top_k)
    )
    if company_id is not None:
        stmt = stmt.where(ChunkRecord.company_id == company_id)

    with get_session() as session:
        rows = session.execute(stmt).all()

    return [
        {
            "company_id": row.company_id,
            "page": row.page_number,
            "document": row.title,
            "file_name": row.file_name,
            "content": row.content,
            "metadata": row.metadata_,
            "distance": row.distance,
        }
        for row in rows
    ]

