from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from app.api.companies import SLUG_PATTERN, URL_PATTERN
from app.db.repository import count_documents
from app.ingestion.manifest import load_sources
from app.ingestion.pipeline import ingest_all
from app.ingestion.uploads import MAX_FILE_BYTES, UploadedFile, register_uploads
from app.services.embeddings import EmbeddingService, get_embedding_service

router = APIRouter(prefix="/ingestion", tags=["ingestion"])


class SourceMetadata(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    company_id: str = Field(pattern=SLUG_PATTERN, max_length=64)
    document_type: str = Field(pattern=SLUG_PATTERN, max_length=100)
    year: int = Field(ge=1900, le=2100)
    title: str = Field(min_length=1, max_length=300)
    source_url: str = Field(pattern=URL_PATTERN, max_length=500)


_METADATA_LIST = TypeAdapter(list[SourceMetadata])


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


@router.post("/upload", status_code=201)
async def upload_documents(
    files: list[UploadFile] = File(...),
    metadata: str = Form(..., description="JSON list with one metadata object per file, in the same order."),
) -> dict[str, Any]:
    """Store files under data/raw/ and register them in sources.yaml. Ingestion is a separate step."""
    try:
        items = _METADATA_LIST.validate_json(metadata)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Read one byte past the limit so oversized files are rejected without loading them fully.
    uploads = [UploadedFile(file.filename or "", await file.read(MAX_FILE_BYTES + 1)) for file in files]
    try:
        sources = register_uploads(uploads, [item.model_dump() for item in items])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"status": "ok", "registered": [source.to_mapping() for source in sources]}
