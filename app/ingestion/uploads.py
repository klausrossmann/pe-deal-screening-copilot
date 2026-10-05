"""User-uploaded source files: saved under data/raw/<company_id>/ and appended to config/sources.yaml.

Nothing is ingested here; the files are picked up by the next ingestion run (POST /ingestion/all).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ingestion.loader import RAW_DATA_DIR
from app.ingestion.manifest import COMPANIES_PATH, SOURCES_PATH, SourceDocument, add_sources, load_companies, load_sources
from app.ingestion.parser import SUPPORTED_SUFFIXES

MAX_FILE_BYTES = 50 * 1024 * 1024
_UNSAFE_CHARS = re.compile(r"[^A-Za-z0-9._-]")


@dataclass
class UploadedFile:
    file_name: str
    content: bytes


def safe_file_name(name: str) -> str:
    """Strip any directory part and replace characters that don't belong in a file name."""
    cleaned = _UNSAFE_CHARS.sub("_", Path(name.replace("\\", "/")).name).lstrip(".")
    if Path(cleaned).suffix.lower() not in SUPPORTED_SUFFIXES:
        allowed = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise ValueError(f"'{name}': unsupported file type (allowed: {allowed}).")
    return cleaned


def register_uploads(
    files: list[UploadedFile],
    metadata: list[dict[str, Any]],
    raw_dir: Path = RAW_DATA_DIR,
    sources_path: str | Path = SOURCES_PATH,
    companies_path: str = COMPANIES_PATH,
) -> list[SourceDocument]:
    """Validate the whole batch first, then write files and manifest entries. Raises ValueError on any problem."""
    if not files:
        raise ValueError("No files uploaded.")
    if len(files) != len(metadata):
        raise ValueError(f"Got {len(files)} files but metadata for {len(metadata)}.")

    known_companies = {company["id"] for company in load_companies(companies_path)}
    taken = {(source.company_id, source.file_name) for source in load_sources(sources_path)}

    sources: list[SourceDocument] = []
    for upload, meta in zip(files, metadata):
        file_name = safe_file_name(upload.file_name)
        company_id = meta["company_id"]
        if company_id not in known_companies:
            raise ValueError(f"'{upload.file_name}': unknown company '{company_id}'. Add the company first.")
        if not upload.content:
            raise ValueError(f"'{upload.file_name}' is empty.")
        if len(upload.content) > MAX_FILE_BYTES:
            raise ValueError(f"'{upload.file_name}' is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB.")
        if file_name.lower().endswith(".pdf") and not upload.content.startswith(b"%PDF"):
            raise ValueError(f"'{upload.file_name}' is not a valid PDF.")
        if (company_id, file_name) in taken or (raw_dir / company_id / file_name).exists():
            raise ValueError(f"'{file_name}' already exists for company '{company_id}'. Rename the file.")
        taken.add((company_id, file_name))

        sources.append(
            SourceDocument(
                company_id=company_id,
                file_name=file_name,
                document_type=meta["document_type"],
                year=int(meta["year"]),
                title=meta["title"],
                source_url=meta["source_url"],
            )
        )

    for upload, source in zip(files, sources):
        target = raw_dir / source.company_id / source.file_name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(upload.content)
    add_sources(sources, sources_path)
    return sources
