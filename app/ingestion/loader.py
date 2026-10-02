from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingestion.manifest import SourceDocument

RAW_DATA_DIR = Path("data/raw")


def resolve_source_path(source: SourceDocument) -> Path:
    path = RAW_DATA_DIR / source.company_id / source.file_name
    if not path.exists():
        raise FileNotFoundError(f"Source file not found: {path}")
    return path


def compute_file_hash(file_path: str | Path) -> str:
    path = Path(file_path)
    hasher = hashlib.sha256()

    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)

    return hasher.hexdigest()
