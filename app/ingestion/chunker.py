from __future__ import annotations

from app.schemas.documents import Chunk, Page


# 500 chars keeps chunks under the default local model's 256-token limit even for
# number-dense slide text, which tokenizes far less efficiently than prose (see ingestion.md).
def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 80) -> list[str]:
    """Split text into windows of at most chunk_size characters, cut at word boundaries."""
    text = " ".join(text.split())
    chunks: list[str] = []
    start = 0

    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            last_space = text.rfind(" ", start, end)
            if last_space > start:
                end = last_space
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break

        # Step back by the overlap (always moving forward), then snap to a word start.
        start = max(end - chunk_overlap, start + 1)
        if text[start - 1] != " " and text[start] != " ":
            next_space = text.find(" ", start)
            start = next_space + 1 if next_space != -1 else len(text)

    return chunks


def chunk_pages(pages: list[Page], chunk_size: int = 500, chunk_overlap: int = 80) -> list[Chunk]:
    """Chunk each page separately so every chunk keeps its page number."""
    chunks: list[Chunk] = []
    for page in pages:
        for content in chunk_text(page.text, chunk_size, chunk_overlap):
            chunks.append(Chunk(page_number=page.page_number, chunk_index=len(chunks), content=content))
    return chunks
