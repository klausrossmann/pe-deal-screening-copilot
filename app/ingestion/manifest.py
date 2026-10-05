from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

COMPANIES_PATH = "config/companies.yaml"
SOURCES_PATH = "config/sources.yaml"


@dataclass
class SourceDocument:
    company_id: str
    file_name: str
    document_type: str
    year: int
    title: str
    source_url: str | None = None

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "SourceDocument":
        return cls(
            company_id=str(data["company_id"]),
            file_name=str(data["file_name"]),
            document_type=str(data["document_type"]),
            year=int(data["year"]),
            title=str(data["title"]),
            source_url=data.get("source_url"),
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "company_id": self.company_id,
            "file_name": self.file_name,
            "document_type": self.document_type,
            "year": self.year,
            "title": self.title,
            "source_url": self.source_url,
        }


def load_sources(path: str | Path = SOURCES_PATH) -> list[SourceDocument]:
    manifest_path = Path(path)
    if not manifest_path.exists():
        raise FileNotFoundError(f"Source manifest not found: {manifest_path}")

    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}

    records = payload.get("sources", [])
    return [SourceDocument.from_mapping(item) for item in records]


@lru_cache
def _load_companies(path: str) -> dict[str, dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return {item["id"]: item for item in payload.get("companies", [])}


def get_company(company_id: str, path: str = COMPANIES_PATH) -> dict[str, Any]:
    companies = _load_companies(path)
    if company_id not in companies:
        raise KeyError(f"Company '{company_id}' is not defined in {path}")
    return companies[company_id]


def load_companies(path: str = COMPANIES_PATH) -> list[dict[str, Any]]:
    return list(_load_companies(path).values())


def add_company(company: dict[str, Any], path: str = COMPANIES_PATH) -> None:
    """Append a company to companies.yaml. Raises ValueError if the id is already taken."""
    if company["id"] in _load_companies(path):
        raise ValueError(f"Company '{company['id']}' already exists.")
    _append_list_items(Path(path), "companies", [company])
    _load_companies.cache_clear()


def add_sources(sources: list[SourceDocument], path: str | Path = SOURCES_PATH) -> None:
    _append_list_items(Path(path), "sources", [source.to_mapping() for source in sources])


def _append_list_items(path: Path, key: str, items: list[dict[str, Any]]) -> None:
    """Append items to the top-level `key:` list as raw text, so the file's comments survive.

    Assumes `key:` is the file's last top-level block (true for both manifests). The result is
    re-parsed and the original restored if the append didn't land in that list.
    """
    original = path.read_text(encoding="utf-8")
    before = len((yaml.safe_load(original) or {}).get(key) or [])

    text = original if original.endswith("\n") else original + "\n"
    for item in items:
        block = yaml.safe_dump([item], sort_keys=False, allow_unicode=True, default_flow_style=False, width=1000)
        text += "\n" + "".join(f"  {line}" for line in block.splitlines(keepends=True))
    path.write_text(text, encoding="utf-8")

    try:
        after = len((yaml.safe_load(text) or {}).get(key) or [])
    except yaml.YAMLError:
        after = -1
    if after != before + len(items):
        path.write_text(original, encoding="utf-8")
        raise ValueError(f"Could not append to '{key}' in {path}; file left unchanged.")
