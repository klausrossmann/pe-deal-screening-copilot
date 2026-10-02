from __future__ import annotations

from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.retrieval.retriever import retrieve
from app.services.embeddings import EmbeddingService
from app.services.llm import ChatService
from app.workflows.common import format_sources_block, to_source_citations

NO_EVIDENCE_ANSWER = "Insufficient evidence: no relevant passages were found in the available sources."

PROMPT_TEMPLATE = """You are helping a private equity analyst answer a question using only the numbered \
sources below. Cite sources inline using their number in brackets, e.g. [1]. If the sources do not contain \
enough information to answer, say so explicitly instead of guessing.

Question: {question}

Sources:
{sources}

Answer:"""


class AskState(TypedDict):
    question: str
    company_id: str | None
    top_k: int
    max_distance: float | None
    chunks: list[dict[str, Any]]
    answer: str


def build_ask_graph(embedding_service: EmbeddingService, chat_service: ChatService):
    """Wires the retrieve -> generate graph; services are injected so callers (and tests) can swap them."""

    async def retrieve_node(state: AskState) -> dict[str, Any]:
        chunks = await retrieve(
            state["question"],
            embedding_service,
            company_id=state.get("company_id"),
            top_k=state.get("top_k", 5),
            max_distance=state.get("max_distance"),
        )
        return {"chunks": chunks}

    async def generate_node(state: AskState) -> dict[str, Any]:
        chunks = state["chunks"]
        if not chunks:
            return {"answer": NO_EVIDENCE_ANSWER}
        prompt = PROMPT_TEMPLATE.format(question=state["question"], sources=format_sources_block(chunks))
        answer = await chat_service.generate(prompt)
        return {"answer": answer}

    graph = StateGraph(AskState)
    graph.add_node("retrieve", retrieve_node)
    graph.add_node("generate", generate_node)
    graph.add_edge(START, "retrieve")
    graph.add_edge("retrieve", "generate")
    graph.add_edge("generate", END)
    return graph.compile()


async def ask(
    question: str,
    embedding_service: EmbeddingService,
    chat_service: ChatService,
    company_id: str | None = None,
    top_k: int = 5,
    max_distance: float | None = None,
) -> dict[str, Any]:
    """Runs the ask graph end-to-end and returns the answer plus the sources it's grounded in."""
    result = await build_ask_graph(embedding_service, chat_service).ainvoke(
        {
            "question": question,
            "company_id": company_id,
            "top_k": top_k,
            "max_distance": max_distance,
            "chunks": [],
            "answer": "",
        }
    )
    return {
        "question": question,
        "company_id": company_id,
        "answer": result["answer"],
        "sources": to_source_citations(result["chunks"]),
    }
