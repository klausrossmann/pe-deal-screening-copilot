from __future__ import annotations

from pathlib import Path
from typing import Any

from app.db.repository import list_companies_with_documents
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import ASSESSMENT_LEVELS, load_screening_config, screen_criterion


def _is_at_least(assessment: str, min_assessment: str) -> bool:
    """ASSESSMENT_LEVELS is ordered strongest-first, so a lower index means stronger evidence."""
    return ASSESSMENT_LEVELS.index(assessment) <= ASSESSMENT_LEVELS.index(min_assessment)


async def screen_universe(
    criterion_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    min_assessment: str = "moderate_evidence",
    config_path: str | Path = "config/screening_config.yaml",
) -> dict[str, Any]:
    """Assesses one screening criterion for every ingested company (UC4), reusing screen_criterion per company."""
    criteria_by_id = {criterion.id: criterion for criterion in load_screening_config(config_path)}
    criterion = criteria_by_id.get(criterion_id)
    if criterion is None:
        raise KeyError(f"Unknown screening criterion '{criterion_id}'. Valid ids: {sorted(criteria_by_id)}")
    if min_assessment not in ASSESSMENT_LEVELS:
        raise ValueError(f"min_assessment must be one of {ASSESSMENT_LEVELS}")

    results = []
    for company_id in list_companies_with_documents():
        result = await screen_criterion(criterion, company_id, embedding_service, chat_service, top_k=top_k)
        results.append({"company_id": company_id, **result})

    matches = [result for result in results if _is_at_least(result["assessment"], min_assessment)]

    return {
        "criterion": criterion.id,
        "question": criterion.question,
        "min_assessment": min_assessment,
        "results": results,
        "matches": matches,
    }
