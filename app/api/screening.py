from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.retrieval.retriever import max_distance_from_env
from app.services.embeddings import EmbeddingService, get_embedding_service
from app.services.llm import ChatService, get_chat_service
from app.workflows.analysis_cache import AnalysisCache, get_analysis_cache
from app.workflows.compare import compare_companies
from app.workflows.screening import screen_company
from app.workflows.universe import screen_universe

router = APIRouter(tags=["screening"])


class ScreeningRequest(BaseModel):
    company_id: str
    top_k: int = 3
    refresh: bool = False  # True = ignore saved results and re-assess


@router.post("/screen")
async def screen(
    request: ScreeningRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    chat_service: ChatService = Depends(get_chat_service),
    cache: AnalysisCache = Depends(get_analysis_cache),
) -> dict[str, Any]:
    try:
        return await screen_company(
            request.company_id,
            embedding_service,
            chat_service,
            top_k=request.top_k,
            max_distance=max_distance_from_env(),
            cache=cache,
            refresh=request.refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class CompareRequest(BaseModel):
    company_a: str
    company_b: str
    top_k: int = 3
    dimension: str | None = None
    refresh: bool = False


@router.post("/compare")
async def compare(
    request: CompareRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    chat_service: ChatService = Depends(get_chat_service),
    cache: AnalysisCache = Depends(get_analysis_cache),
) -> dict[str, Any]:
    try:
        return await compare_companies(
            request.company_a,
            request.company_b,
            embedding_service,
            chat_service,
            top_k=request.top_k,
            dimension=request.dimension,
            max_distance=max_distance_from_env(),
            cache=cache,
            refresh=request.refresh,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


class UniverseScreeningRequest(BaseModel):
    criteria: list[str]
    top_k: int = 3
    min_assessment: str = "moderate_evidence"
    refresh: bool = False


@router.post("/screen/universe")
async def screen_universe_endpoint(
    request: UniverseScreeningRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    chat_service: ChatService = Depends(get_chat_service),
    cache: AnalysisCache = Depends(get_analysis_cache),
) -> dict[str, Any]:
    try:
        return await screen_universe(
            request.criteria,
            embedding_service,
            chat_service,
            top_k=request.top_k,
            min_assessment=request.min_assessment,
            max_distance=max_distance_from_env(),
            cache=cache,
            refresh=request.refresh,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
