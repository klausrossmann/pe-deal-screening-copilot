from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, TypedDict

import yaml
from langgraph.graph import END, START, StateGraph

from app.db.repository import company_corpus_fingerprint
from app.retrieval.retriever import retrieve_multi
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.analysis_cache import AnalysisCache, fingerprint
from app.workflows.common import format_sources_block, to_source_citations

ASSESSMENT_LEVELS = ("strong_evidence", "moderate_evidence", "weak_evidence", "insufficient_evidence")
POLARITIES = ("positive", "risk")
INSUFFICIENT_EVIDENCE_RATIONALE = "No relevant passages were found in the available sources."
UNPARSEABLE_RATIONALE = "The model's response could not be parsed into a structured assessment."

RISK_NOTE = """

This criterion describes a RISK. Classify how strongly the sources show that this risk is present for the \
company: strong_evidence means the risk is clearly present (a red flag), not that the company is strong."""

PROMPT_TEMPLATE = """You are a private equity analyst assessing evidence for one screening criterion, using \
only the numbered sources below. Do not use any outside knowledge, and do not invent a numeric score.

Criterion: {question}{risk_note}

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
    polarity: str = "positive"
    retrieval_queries: tuple[str, ...] = ()

    @property
    def queries(self) -> list[str]:
        """Everything that gets searched: the question itself, then any extra retrieval phrases."""
        return [self.question, *self.retrieval_queries]


def load_screening_config(path: str | Path = "config/screening_config.yaml") -> list[Criterion]:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Screening config not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    criteria: list[Criterion] = []
    for dimension_id, dimension in (payload.get("screening_dimensions") or {}).items():
        description = dimension.get("description", "")
        polarity = dimension.get("polarity", "positive")
        if polarity not in POLARITIES:
            raise ValueError(f"Dimension '{dimension_id}' has polarity '{polarity}'; use one of {POLARITIES}.")
        for criterion_id, criterion in (dimension.get("criteria") or {}).items():
            criteria.append(
                Criterion(
                    id=criterion_id,
                    question=str(criterion["question"]),
                    dimension=dimension_id,
                    dimension_description=description,
                    polarity=polarity,
                    retrieval_queries=tuple(str(q) for q in criterion.get("retrieval_queries") or ()),
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
    queries: list[str]
    polarity: str
    company_id: str | None
    top_k: int
    max_distance: float | None
    chunks: list[dict[str, Any]]
    assessment: str
    rationale: str


# Compiled once per pair of services and reused for every criterion and company, instead of once per call.
@lru_cache(maxsize=8)
def build_screen_criterion_graph(embedding_service: EmbeddingService, chat_service: ChatService):
    """Wires the retrieve -> assess graph for a single criterion; same shape as the ask graph."""

    async def retrieve_node(state: CriterionState) -> dict[str, Any]:
        chunks = await retrieve_multi(
            state["queries"],
            embedding_service,
            company_id=state.get("company_id"),
            top_k=state.get("top_k", 3),
            max_distance=state.get("max_distance"),
        )
        return {"chunks": chunks}

    async def assess_node(state: CriterionState) -> dict[str, Any]:
        chunks = state["chunks"]
        if not chunks:
            return {"assessment": "insufficient_evidence", "rationale": INSUFFICIENT_EVIDENCE_RATIONALE}
        prompt = PROMPT_TEMPLATE.format(
            question=state["question"],
            risk_note=RISK_NOTE if state["polarity"] == "risk" else "",
            sources=format_sources_block(chunks),
        )
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
    max_distance: float | None = None,
    cache: AnalysisCache | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """Assesses one criterion for one company.

    1. If a `cache` is given and holds an up-to-date result, return it (no retrieval, no LLM call).
    2. Otherwise run the retrieve -> assess graph.
    3. Save the new result to the cache for next time.

    `refresh=True` skips step 1 but still saves the new result.
    """
    current_fingerprint = None
    if cache is not None:
        current_fingerprint = await _cache_fingerprint(
            criterion, company_id, embedding_service, chat_service, top_k, max_distance
        )
        saved = None if refresh else cache.load(company_id, criterion.id, current_fingerprint)
        if saved is not None:
            return {**saved, "cached": True, "error": False}

    result = await build_screen_criterion_graph(embedding_service, chat_service).ainvoke(
        {
            "question": criterion.question,
            "queries": criterion.queries,
            "polarity": criterion.polarity,
            "company_id": company_id,
            "top_k": top_k,
            "max_distance": max_distance,
            "chunks": [],
            "assessment": "",
            "rationale": "",
        }
    )
    assessment = {
        "criterion": criterion.id,
        "dimension": criterion.dimension,
        "polarity": criterion.polarity,
        "question": criterion.question,
        "assessment": result["assessment"],
        "rationale": result["rationale"],
        "sources": to_source_citations(result["chunks"]),
    }
    if cache is not None:
        cache.save(company_id, criterion.id, current_fingerprint, assessment)
    return {**assessment, "cached": False, "error": False}


async def screen_criterion_safe(
    criterion: Criterion,
    company_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    **kwargs: Any,
) -> dict[str, Any]:
    """Same as `screen_criterion`, but never raises.

    Used wherever many criteria run concurrently (`screen_company`, `screen_universe`): one criterion hitting a
    transient failure (e.g. an LLM rate limit outlasting the SDK's own retries) would otherwise fail the whole
    `asyncio.gather` and turn a mostly-successful report into a 500, discarding results that already succeeded.
    A failed criterion becomes `insufficient_evidence` with `"error": True` and is not saved to the cache, so
    it's retried (not treated as a real assessment) next time.
    """
    try:
        return await screen_criterion(criterion, company_id, embedding_service, chat_service, **kwargs)
    except Exception as exc:  # noqa: BLE001 - deliberately broad: any failure degrades, none crash the report
        return {
            "criterion": criterion.id,
            "dimension": criterion.dimension,
            "polarity": criterion.polarity,
            "question": criterion.question,
            "assessment": "insufficient_evidence",
            "rationale": f"Assessment failed and was not saved: {type(exc).__name__}: {exc}",
            "sources": [],
            "cached": False,
            "error": True,
        }


async def _cache_fingerprint(
    criterion: Criterion,
    company_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int,
    max_distance: float | None,
) -> str:
    """Everything that can change an assessment; if any of it changes, the saved result is ignored."""
    corpus = await asyncio.to_thread(company_corpus_fingerprint, company_id)
    return fingerprint(
        {
            "queries": criterion.queries,
            "polarity": criterion.polarity,
            "prompt": PROMPT_TEMPLATE + RISK_NOTE,
            "top_k": top_k,
            "max_distance": max_distance,
            "embedding_model": _model_id(embedding_service),
            "llm_model": _model_id(chat_service),
            "corpus": corpus,
        }
    )


def _model_id(service: Any) -> str:
    return f"{getattr(service, 'provider', type(service).__name__)}/{getattr(service, 'model_name', '')}"


async def screen_company(
    company_id: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    top_k: int = 3,
    config_path: str | Path = "config/screening_config.yaml",
    dimension: str | None = None,
    max_distance: float | None = None,
    cache: AnalysisCache | None = None,
    refresh: bool = False,
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

    # All criteria run concurrently; ChatService limits how many LLM calls are actually in flight.
    # screen_criterion_safe means one criterion failing (e.g. a rate limit) still lets the rest of the report through.
    results = await asyncio.gather(
        *(
            screen_criterion_safe(
                criterion, company_id, embedding_service, chat_service,
                top_k=top_k, max_distance=max_distance, cache=cache, refresh=refresh,
            )
            for criterion in criteria
        )
    )

    dimensions: dict[str, dict[str, Any]] = {}
    for criterion, result in zip(criteria, results):
        group = dimensions.setdefault(
            criterion.dimension,
            {
                "dimension": criterion.dimension,
                "description": criterion.dimension_description,
                "polarity": criterion.polarity,
                "criteria": [],
            },
        )
        group["criteria"].append(result)

    return {"company_id": company_id, "dimensions": list(dimensions.values())}
