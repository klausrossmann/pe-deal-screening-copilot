from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company

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
