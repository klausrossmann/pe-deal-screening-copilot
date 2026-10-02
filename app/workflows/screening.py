from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph

from app.retrieval.retriever import retrieve
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.common import format_sources_block, to_source_citations

ASSESSMENT_LEVELS = ("strong_evidence", "moderate_evidence", "weak_evidence", "insufficient_evidence")
INSUFFICIENT_EVIDENCE_RATIONALE = "No relevant passages were found in the available sources."
UNPARSEABLE_RATIONALE = "The model's response could not be parsed into a structured assessment."

PROMPT_TEMPLATE = """You are a private equity analyst assessing evidence for one screening criterion, using \
only the numbered sources below. Do not use any outside knowledge, and do not invent a numeric score.

Criterion: {question}

Sources:
{sources}

Classify the strength of evidence for this criterion and explain your reasoning in 2-4 sentences, citing \
sources inline using their number in brackets, e.g. [1]. If the sources do not provide enough information, \
classify it as insufficient_evidence instead of guessing.

Respond with ONLY a JSON object with exactly these two keys, nothing else:
{{"assessment": "strong_evidence" | "moderate_evidence" | "weak_evidence" | "insufficient_evidence", \
"rationale": "..."}}"""


@dataclass
class Criterion:
    id: str
    question: str
    dimension: str
    dimension_description: str


def load_screening_config(path: str | Path = "config/screening_config.yaml") -> list[Criterion]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Screening config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    criteria: list[Criterion] = []
    for dimension_id, dimension in (payload.get("screening_dimensions") or {}).items():
        description = dimension.get("description", "")
        for criterion_id, criterion in (dimension.get("criteria") or {}).items():
            criteria.append(
                Criterion(
                    id=criterion_id,
                    question=str(criterion["question"]),
                    dimension=dimension_id,
                    dimension_description=description,
                )
            )
    return criteria


def _parse_assessment(raw: str) -> dict[str, str]:
    """Parses the LLM's JSON reply, tolerating code fences or stray text around it."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    candidate = match.group(0) if match else raw

    assessment = ""
    rationale = ""
    try:
        data = json.loads(candidate)
        assessment = str(data.get("assessment", "")).strip().lower()
        rationale = str(data.get("rationale", "")).strip()
    except (json.JSONDecodeError, AttributeError):
        pass

    if assessment not in ASSESSMENT_LEVELS:
        return {"assessment": "insufficient_evidence", "rationale": rationale or UNPARSEABLE_RATIONALE}
    return {"assessment": assessment, "rationale": rationale}


class CriterionState(TypedDict):
    question: str
    company_id: str | None
    top_k: int
    chunks: list[dict[str, Any]]
    assessment: str
    rationale: str


def build_screen_criterion_graph(embedding_service: EmbeddingService, chat_service: ChatService):
    """Wires the retrieve -> assess graph for a single criterion; same shape as the ask graph."""

    async def retrieve_node(state: CriterionState) -> dict[str, Any]:
        chunks = await retrieve(
            state["question"],
            embedding_service,
            company_id=state.get("company_id"),
            top_k=state.get("top_k", 3),
        )
        return {"chunks": chunks}

    async def assess_node(state: CriterionState) -> dict[str, Any]:
        chunks = state["chunks"]
        if not chunks:
            return {"assessment": "insufficient_evidence", "rationale": INSUFFICIENT_EVIDENCE_RATIONALE}
        prompt = PROMPT_TEMPLATE.format(question=state["question"], sources=format_sources_block(chunks))
        raw = await chat_service.generate(prompt)
        return _parse_assessment(raw)

    graph = StateGraph(CriterionState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("assess", assess_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "assess")
    graph.add_edge("assess", END)
    return graph.compile()


async def screen_criterion(
    criterion: Criterion,
    company_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
) -> dict[str, Any]:
    """Runs the retrieve -> assess graph for one criterion and returns a structured assessment."""
    result = await build_screen_criterion_graph(embedding_service, chat_service).ainvoke(
        {
            "question": criterion.question,
            "company_id": company_id,
            "top_k": top_k,
            "chunks": [],
            "assessment": "",
            "rationale": "",
        }
    )
    return {
        "criterion": criterion.id,
        "dimension": criterion.dimension,
        "question": criterion.question,
        "assessment": result["assessment"],
        "rationale": result["rationale"],
        "sources": to_source_citations(result["chunks"]),
    }


async def screen_company(
    company_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    config_path: str | Path = "config/screening_config.yaml",
    dimension: str | None = None,
) -> dict[str, Any]:
    """Assesses every criterion in the screening config for one company, grouped by dimension.

    If `dimension` is given, only that dimension's criteria are assessed (used by `compare_companies()`
    to avoid running criteria outside the dimension the caller asked about).
    """
    criteria = load_screening_config(config_path)
    if dimension is not None:
        known_dimensions = {criterion.dimension for criterion in criteria}
        if dimension not in known_dimensions:
            raise KeyError(f"Unknown screening dimension '{dimension}'. Valid ids: {sorted(known_dimensions)}")
        criteria = [criterion for criterion in criteria if criterion.dimension == dimension]

    dimensions: dict[str, dict[str, Any]] = {}
    for criterion in criteria:
        result = await screen_criterion(criterion, company_id, embedding_service, chat_service, top_k=top_k)
        dimension = dimensions.setdefault(
            criterion.dimension, {"dimension": criterion.dimension, "description": criterion.dimension_description, "criteria": []}
        )
        dimension["criteria"].append(result)

    return {"company_id": company_id, "dimensions": list(dimensions.values())}
