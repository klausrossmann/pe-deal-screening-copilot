from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


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


def load_sources(path: str | Path = "config/sources.yaml") -> list[SourceDocument]:
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


def get_company(company_id: str, path: str = "config/companies.yaml") -> dict[str, Any]:
    companies = _load_companies(path)
    if company_id not in companies:
        raise KeyError(f"Company '{company_id}' is not defined in {path}")
    return companies[company_id]
