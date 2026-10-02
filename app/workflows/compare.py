from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.screening import screen_company


async def compare_companies(
    company_a_id: str,
    company_b_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    dimension: str | None = None,
    config_path: str | Path = "config/screening_config.yaml",
) -> dict[str, Any]:
    """Applies the same screening criteria to two companies side by side (UC3), reusing screen_company()."""
    result_a = await screen_company(
        company_a_id, embedding_service, chat_service, top_k=top_k, config_path=config_path, dimension=dimension
    )
    result_b = await screen_company(
        company_b_id, embedding_service, chat_service, top_k=top_k, config_path=config_path, dimension=dimension
    )

    dimensions = []
    criteria_b_by_dimension = {d["dimension"]: d["criteria"] for d in result_b["dimensions"]}
    for dim_a in result_a["dimensions"]:
        criteria_b_by_id = {c["criterion"]: c for c in criteria_b_by_dimension[dim_a["dimension"]]}
        criteria = [
            {
                "criterion": criterion_a["criterion"],
                "question": criterion_a["question"],
                "company_a": {
                    "assessment": criterion_a["assessment"],
                    "rationale": criterion_a["rationale"],
                    "sources": criterion_a["sources"],
                },
                "company_b": {
                    "assessment": criteria_b_by_id[criterion_a["criterion"]]["assessment"],
                    "rationale": criteria_b_by_id[criterion_a["criterion"]]["rationale"],
                    "sources": criteria_b_by_id[criterion_a["criterion"]]["sources"],
                },
            }
            for criterion_a in dim_a["criteria"]
        ]
        dimensions.append({"dimension": dim_a["dimension"], "description": dim_a["description"], "criteria": criteria})

    return {
        "company_a": company_a_id,
        "company_b": company_b_id,
        "dimension": dimension,
        "dimensions": dimensions,
    }
