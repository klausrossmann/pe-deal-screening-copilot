from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.db.repository import list_companies_with_documents
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.analysis_cache import AnalysisCache
from app.workflows.screening import ASSESSMENT_LEVELS, load_screening_config, screen_criterion


def _is_at_least(assessment: str, min_assessment: str) -> bool:
    """ASSESSMENT_LEVELS is ordered strongest-first, so a lower index means stronger evidence."""
    return ASSESSMENT_LEVELS.index(assessment) <= ASSESSMENT_LEVELS.index(min_assessment)


async def screen_universe(
    criterion_ids: str | list[str],
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    min_assessment: str = "moderate_evidence",
    config_path: str | Path = "config/screening_config.yaml",
    max_distance: float | None = None,
    cache: AnalysisCache | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Assesses one or more screening criteria for every ingested company (UC4).

    Returns one flat row per (company, criterion). `meets_threshold` marks rows whose evidence is at least
    `min_assessment`; `matches` is just those rows. For risk criteria, a match means the risk was flagged.
    """
    if isinstance(criterion_ids, str):
        criterion_ids = [criterion_ids]
    if not criterion_ids:
        raise ValueError("Pass at least one criterion id.")
    criteria_by_id = {criterion.id: criterion for criterion in load_screening_config(config_path)}
    unknown = [criterion_id for criterion_id in criterion_ids if criterion_id not in criteria_by_id]
    if unknown:
        raise KeyError(f"Unknown screening criterion {unknown}. Valid ids: {sorted(criteria_by_id)}")
    if min_assessment not in ASSESSMENT_LEVELS:
        raise ValueError(f"min_assessment must be one of {ASSESSMENT_LEVELS}")
    criteria = [criteria_by_id[criterion_id] for criterion_id in criterion_ids]

    pairs = [(company_id, criterion) for company_id in list_companies_with_documents() for criterion in criteria]
    # Everything runs concurrently; ChatService limits how many LLM calls are actually in flight.
    assessments = await asyncio.gather(
        *(
            screen_criterion(
                criterion, company_id, embedding_service, chat_service,
                top_k=top_k, max_distance=max_distance, cache=cache, refresh=refresh,
            )
            for company_id, criterion in pairs
        )
    )

    results = [
        {"company_id": company_id, **result, "meets_threshold": _is_at_least(result["assessment"], min_assessment)}
        for (company_id, _), result in zip(pairs, assessments)
    ]

    return {
        "criteria": [
            {
                "criterion": criterion.id,
                "dimension": criterion.dimension,
                "polarity": criterion.polarity,
                "question": criterion.question,
            }
            for criterion in criteria
        ],
        "min_assessment": min_assessment,
        "results": results,
        "matches": [result for result in results if result["meets_threshold"]],
    }
