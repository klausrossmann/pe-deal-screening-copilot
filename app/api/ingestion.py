from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.db.repository import count_documents
from app.ingestion.manifest import load_sources
from app.ingestion.pipeline import ingest_all
from app.services.embeddings import EmbeddingService, get_embedding_service

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


@router.get("/status")
async def status() -> dict[str, Any]:
    return {"status": "ready", "documents": count_documents()}


@router.post("/all")
async def ingest_all_documents(
    embedding_service: EmbeddingService = Depends(get_embedding_service),
) -> dict[str, Any]:
    results = await ingest_all(load_sources(), embedding_service)
    return {
        "status": "ok",
        "ingested": sum(r["ingested"] for r in results),
        "skipped": sum(not r["ingested"] for r in results),
        "documents": results,
    }
