from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Page:
    page_number: int
    text: str


@dataclass
class Chunk:
    page_number: int
    chunk_index: int
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] = field(default_factory=list)
