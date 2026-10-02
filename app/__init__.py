"""Application package for the PE deal screening ingestion pipeline."""
from __future__ import annotations

from dotenv import load_dotenv

# Runs on the first `import app...`, so uvicorn/pytest/`python -m app...` all see .env without manually
# sourcing it first. Walks up from this file to find .env, so it works regardless of the current directory.
# Variables already set in the shell still win (load_dotenv never overrides by default).
load_dotenv()
