from __future__ import annotations

import asyncio
import os
from typing import Any

from sqlalchemy import select

from app.db.models import ChunkRecord, Document
from app.db.session import get_session
from app.services.embeddings import EmbeddingService


def max_distance_from_env() -> float | None:
    """Reads RETRIEVAL_MAX_DISTANCE; unset means no cutoff."""
    value = os.getenv("RETRIEVAL_MAX_DISTANCE", "").strip()
    return float(value) if value else None


async def retrieve(
    query: str,
    embedding_service: EmbeddingService,
    company_id: str | None = None,
    top_k: int = 5,
    max_distance: float | None = None,
) -> list[dict[str, Any]]:
    """Return the top_k chunks closest to the query, using pgvector cosine distance.

    With `max_distance`, chunks further away than that are dropped, so an unrelated query can return nothing.
    """
    query_embedding = await embedding_service.embed_query(query)
    distance_expr = ChunkRecord.embedding.cosine_distance(query_embedding)
    distance = distance_expr.label("distance")

    stmt = (
        select(
            ChunkRecord.id,
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
    if max_distance is not None:
        stmt = stmt.where(distance_expr <= max_distance)

    # The DB driver is synchronous; a worker thread keeps the event loop free for concurrent searches.
    rows = await asyncio.to_thread(_fetch_rows, stmt)

    return [
        {
            "chunk_id": row.id,
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


async def retrieve_multi(
    queries: list[str],
    embedding_service: EmbeddingService,
    company_id: str | None = None,
    top_k: int = 5,
    max_distance: float | None = None,
) -> list[dict[str, Any]]:
    """Runs one search per query and merges them: each chunk appears once, ranked by its best distance."""
    per_query = await asyncio.gather(
        *(retrieve(query, embedding_service, company_id, top_k, max_distance) for query in queries)
    )
    best: dict[str, dict[str, Any]] = {}
    for chunks in per_query:
        for chunk in chunks:
            kept = best.get(chunk["chunk_id"])
            if kept is None or chunk["distance"] < kept["distance"]:
                best[chunk["chunk_id"]] = chunk
    return sorted(best.values(), key=lambda chunk: chunk["distance"])[:top_k]


def _fetch_rows(stmt) -> list[Any]:
    with get_session() as session:
        return session.execute(stmt).all()

