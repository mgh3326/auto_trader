"""Read-only /invest common/preferred-share disparity schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

DisparityState = Literal["fresh", "partial", "stale", "missing"]
DisparityTone = Literal["discount", "premium", "parity", "unknown"]


class DisparitySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    sourceOfTruth: str
    asOf: datetime | None = None
    stale: bool = False
    freshnessSec: int | None = None
    warnings: list[str] = Field(default_factory=list)


class DisparityPeriodWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    period: Literal["1d", "5d", "20d", "60d"]
    sampleCount: int
    meanDisparityPct: float | None = None
    minDisparityPct: float | None = None
    maxDisparityPct: float | None = None
    zScore: float | None = None
    dataState: DisparityState
    emptyReason: str | None = None


class CommonPreferredDisparityCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    commonSymbol: str
    commonName: str
    preferredSymbol: str
    preferredName: str
    exchange: str | None = None
    commonPrice: float | None = None
    preferredPrice: float | None = None
    disparityPct: float | None = None
    preferredDiscountPct: float | None = None
    preferredPremiumPct: float | None = None
    zScore: float | None = None
    primaryWindow: Literal["1d", "5d", "20d", "60d"] = "20d"
    windows: list[DisparityPeriodWindow] = Field(default_factory=list)
    tone: DisparityTone = "unknown"
    dataState: DisparityState
    emptyReason: str | None = None
    formula: str = "((commonPrice - preferredPrice) / commonPrice) * 100"
    source: DisparitySource
    warnings: list[str] = Field(default_factory=list)
    caution: str = (
        "보통주/우선주 괴리율은 참고용 정보이며 매수·매도 신호가 아닙니다. "
        "배당, 유동성, 의결권, 세금, 이벤트 리스크를 별도로 확인하세요."
    )


class CommonPreferredDisparityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr"] = "kr"
    state: DisparityState
    asOf: datetime
    cards: list[CommonPreferredDisparityCard] = Field(default_factory=list)
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(
        default_factory=lambda: [
            "Read-only disparity dashboard; no broker/order/watch mutations.",
            "raoni.xyz is benchmark-only and is not queried in production.",
            "Common/preferred pair mapping is heuristic until a curated mapping table is approved.",
        ]
    )
