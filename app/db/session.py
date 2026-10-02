from __future__ import annotations

import os
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

DEFAULT_URL = "postgresql+psycopg2://postgres:postgres@localhost:5432/pe_screening"


@lru_cache
def get_engine() -> Engine:
    return create_engine(os.getenv("POSTGRES_URL", DEFAULT_URL))


def get_session() -> Session:
    return Session(get_engine())
