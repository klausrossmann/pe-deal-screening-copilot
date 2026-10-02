from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company
from app.workflows.universe import screen_universe

router = APIRouter(tags=["screening"])


class ScreeningRequest(BaseModel):
    company_id: str
    top_k: int = 3


@router.post("/screen")
async def screen(request: ScreeningRequest) -> dict[str, Any]:
    return await screen_company(
        request.company_id,
        EmbeddingService(),
        ChatService(),
        top_k=request.top_k,
    )


class UniverseScreeningRequest(BaseModel):
    criterion: str
    top_k: int = 3
    min_assessment: str = "moderate_evidence"


@router.post("/screen/universe")
async def screen_universe_endpoint(request: UniverseScreeningRequest) -> dict[str, Any]:
    try:
        return await screen_universe(
            request.criterion,
            EmbeddingService(),
            ChatService(),
            top_k=request.top_k,
            min_assessment=request.min_assessment,
        )
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
