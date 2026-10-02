from __future__ import annotations

import sys

from sqlalchemy import text

from app.db.models import Base
from app.db.session import get_engine


def init_db(reset: bool = False) -> None:
    """Create the pgvector extension and all tables. reset=True drops existing data first."""
    engine = get_engine()

    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))

    if reset:
        Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


if __name__ == "__main__":
    init_db(reset="--reset" in sys.argv)
    print("Database initialized")
