from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.retrieval.retriever import max_distance_from_env
from app.services.embeddings import EmbeddingService, get_embedding_service
from app.services.llm import ChatService, get_chat_service
from app.workflows.ask import ask

router = APIRouter(tags=["ask"])


class AskRequest(BaseModel):
    question: str
    company_id: str | None = None
    top_k: int = 5


@router.post("/ask")
async def ask_question(
    request: AskRequest,
    embedding_service: EmbeddingService = Depends(get_embedding_service),
    chat_service: ChatService = Depends(get_chat_service),
) -> dict[str, Any]:
    return await ask(
        request.question,
        embedding_service,
        chat_service,
        company_id=request.company_id,
        top_k=request.top_k,
        max_distance=max_distance_from_env(),
    )
