from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.analysis_cache import AnalysisCache
from app.workflows.screening import screen_company


async def compare_companies(
    company_a_id: str,
    company_b_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    dimension: str | None = None,
    config_path: str | Path = "config/screening_config.yaml",
    max_distance: float | None = None,
    cache: AnalysisCache | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Applies the same screening criteria to two companies side by side (UC3), reusing screen_company()."""
    options = {
        "top_k": top_k,
        "config_path": config_path,
        "dimension": dimension,
        "max_distance": max_distance,
        "cache": cache,
        "refresh": refresh,
    }
    result_a, result_b = await asyncio.gather(
        screen_company(company_a_id, embedding_service, chat_service, **options),
        screen_company(company_b_id, embedding_service, chat_service, **options),
    )

    dimensions = []
    criteria_b_by_dimension = {d["dimension"]: d["criteria"] for d in result_b["dimensions"]}
    for dim_a in result_a["dimensions"]:
        criteria_b_by_id = {c["criterion"]: c for c in criteria_b_by_dimension[dim_a["dimension"]]}
        criteria = [
            {
                "criterion": criterion_a["criterion"],
                "question": criterion_a["question"],
                "polarity": dim_a.get("polarity", "positive"),
                "company_a": _side(criterion_a),
                "company_b": _side(criteria_b_by_id[criterion_a["criterion"]]),
            }
            for criterion_a in dim_a["criteria"]
        ]
        dimensions.append(
            {
                "dimension": dim_a["dimension"],
                "description": dim_a["description"],
                "polarity": dim_a.get("polarity", "positive"),
                "criteria": criteria,
            }
        )

    return {
        "company_a": company_a_id,
        "company_b": company_b_id,
        "dimension": dimension,
        "dimensions": dimensions,
    }


def _side(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "assessment": result["assessment"],
        "rationale": result["rationale"],
        "sources": result["sources"],
        "cached": result.get("cached", False),
        "error": result.get("error", False),
    }
