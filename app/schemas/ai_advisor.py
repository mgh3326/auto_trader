"""AI Advisor request/response schemas."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.schemas.ai_markdown import PresetType

DISCLAIMER = "AI 분석 보조 도구이며 투자 자문이 아닙니다."


class AiAdviceRequest(BaseModel):
    """AI advice request."""

    scope: Literal["portfolio", "position"]
    preset: PresetType
    provider: str
    model: str | None = None
    question: str = Field(..., min_length=1, max_length=2000)
    # position scope
    market_type: str | None = None
    symbol: str | None = None
    # portfolio scope
    include_market: str = "ALL"


class AiAdviceResponse(BaseModel):
    """AI advice response."""

    success: bool
    answer: str
    provider: str
    model: str
    usage: dict[str, Any] | None = None
    elapsed_ms: int
    error: str | None = None
    disclaimer: str = DISCLAIMER


class ProviderInfo(BaseModel):
    """Single provider info."""

    name: str
    default_model: str


class AiProvidersResponse(BaseModel):
    """Available providers response."""

    providers: list[ProviderInfo]
    default_provider: str
