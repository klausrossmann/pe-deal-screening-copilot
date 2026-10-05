from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.ask import router as ask_router
from app.api.companies import router as companies_router
from app.api.ingestion import router as ingestion_router
from app.api.screening import router as screening_router
from app.db.bootstrap import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="PE Deal Screening Ingestion API", lifespan=lifespan)
app.include_router(ingestion_router)
app.include_router(companies_router)
app.include_router(ask_router)
app.include_router(screening_router)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}
