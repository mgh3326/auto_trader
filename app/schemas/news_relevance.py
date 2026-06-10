"""Request/response contracts for the news-relevance judgment surface (ROB-491)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class NewsRelevanceJudgment(BaseModel):
    article_id: int
    market: Literal["kr", "us", "crypto"]
    symbol: str = Field(min_length=1, max_length=40)
    relationship: Literal["direct", "material_indirect", "incidental", "unrelated"]
    relevance: Literal["high", "medium", "low"]
    price_relevance: Literal["catalyst", "explainer", "background", "none"]
    score: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = Field(min_length=1, max_length=2000)
    judged_by: str = Field(min_length=1, max_length=100)


class NewsRelevanceIngestRequest(BaseModel):
    judgments: list[NewsRelevanceJudgment] = Field(min_length=1, max_length=200)
