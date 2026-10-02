from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.ask import ask

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str
    company_id: str | None = None
    top_k: int = 5


@router.post("/ask")
async def ask_question(request: AskRequest) -> dict[str, Any]:
    return await ask(
        request.question,
        EmbeddingService(),
        ChatService(),
        company_id=request.company_id,
        top_k=request.top_k,
    )
