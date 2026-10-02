"""Saved screening results (framework.md section 6): one JSON file per company and criterion.

    data/analysis/<company_id>/<criterion_id>.json

A saved result is reused only while its fingerprint matches, i.e. while nothing that influenced it has changed
(question, retrieval settings, prompt, models, or the company's ingested documents).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_ANALYSIS_DIR = "data/analysis"
# Ids become path segments, so only plain ids are allowed (no "../", no slashes).
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]+")


def fingerprint(inputs: dict[str, Any]) -> str:
    """Stable hash of everything that influences an assessment."""
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class AnalysisCache:
    def __init__(self, directory: str | Path = DEFAULT_ANALYSIS_DIR):
        self.directory = Path(directory)

    def load(self, company_id: str, criterion_id: str, expected_fingerprint: str) -> dict[str, Any] | None:
        """Returns the saved result, or None if there is none or it is stale."""
        try:
            saved = json.loads(self._path(company_id, criterion_id).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return None
        if saved.get("fingerprint") != expected_fingerprint:
            return None
        return saved["result"]

    def save(self, company_id: str, criterion_id: str, current_fingerprint: str, result: dict[str, Any]) -> None:
        path = self._path(company_id, criterion_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "company_id": company_id,
            "criterion": criterion_id,
            "fingerprint": current_fingerprint,
            "assessed_at": datetime.now(timezone.utc).isoformat(),
            "result": result,
        }
        # Write to a temp file and rename, so a crash never leaves a half-written JSON file behind.
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8") as tmp:
            json.dump(payload, tmp, indent=2)
        os.replace(tmp.name, path)

    def _path(self, company_id: str, criterion_id: str) -> Path:
        for value in (company_id, criterion_id):
            if not _SAFE_ID.fullmatch(value):
                raise ValueError(f"Invalid id {value!r}: only letters, digits, '_' and '-' are allowed.")
        return self.directory / company_id / f"{criterion_id}.json"


@lru_cache
def get_analysis_cache() -> AnalysisCache:
    return AnalysisCache(os.getenv("ANALYSIS_DIR", DEFAULT_ANALYSIS_DIR))
