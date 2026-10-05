from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.ingestion.manifest import add_company, load_companies

router = APIRouter(prefix="/companies", tags=["companies"])

SLUG_PATTERN = r"^[a-z0-9][a-z0-9_]*$"
URL_PATTERN = r"^https?://\S+$"


class CompanyIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(pattern=SLUG_PATTERN, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    website: str = Field(pattern=URL_PATTERN, max_length=500)
    country: str = Field(min_length=1, max_length=100)
    category: str = Field(pattern=SLUG_PATTERN, max_length=100)
    ownership: Literal["public", "private"]
    ticker: str | None = Field(default=None, max_length=20)
    screening_tags: list[Annotated[str, Field(pattern=SLUG_PATTERN, max_length=100)]] = Field(min_length=1)

    @field_validator("ticker")
    @classmethod
    def _blank_ticker_to_none(cls, value: str | None) -> str | None:
        return value or None


@router.get("")
async def list_companies() -> list[dict[str, Any]]:
    return load_companies()


@router.post("", status_code=201)
async def create_company(company: CompanyIn) -> dict[str, Any]:
    payload = company.model_dump()
    try:
        add_company(payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return payload
