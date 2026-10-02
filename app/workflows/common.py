from __future__ import annotations

from typing import Any


def format_sources_block(chunks: list[dict[str, Any]]) -> str:
    """Formats retrieved chunks into a numbered block for an LLM prompt, e.g. "[1] Title (file, p. 3): ...". """
    return "\n\n".join(
        f"[{i}] {chunk['document']} ({chunk['file_name']}, p. {chunk['page']}):\n{chunk['content']}"
        for i, chunk in enumerate(chunks, start=1)
    )


def to_source_citations(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Turns retrieved chunks into the citation dicts returned to callers (document, file, page, distance)."""
    return [
        {
            "document": chunk["document"],
            "file_name": chunk["file_name"],
            "page": chunk["page"],
            "distance": chunk["distance"],
        }
        for chunk in chunks
    ]
