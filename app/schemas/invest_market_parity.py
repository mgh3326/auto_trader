"""ROB-251 — read-only /invest market parity schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InvestParityState = Literal["fresh", "partial", "stale", "missing", "disabled"]
InvestParityTone = Literal["premium", "discount", "flat", "unknown"]
InvestParityCardType = Literal[
    "index_implied_parity",
    "stablecoin_fx_premium",
    "crypto_kimchi_premium",
    "synthetic_kr_stock_parity",
]


class InvestParitySource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    sourceOfTruth: str
    asOf: datetime | None = None
    stale: bool = False
    freshnessSec: int | None = None
    warnings: list[str] = Field(default_factory=list)


class InvestMarketParityCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    type: InvestParityCardType
    title: str
    baseSymbol: str | None = None
    baseName: str | None = None
    proxySymbol: str | None = None
    syntheticSymbol: str | None = None
    basePrice: float | None = None
    proxyPrice: float | None = None
    syntheticPrice: float | None = None
    fxRate: float | None = None
    usdtKrw: float | None = None
    usdKrw: float | None = None
    impliedValue: float | None = None
    premiumPct: float | None = None
    tone: InvestParityTone = "unknown"
    formula: str | None = None
    dataState: InvestParityState
    emptyReason: str | None = None
    source: InvestParitySource


class InvestMarketParityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr"] = "kr"
    state: InvestParityState
    asOf: datetime
    cards: list[InvestMarketParityCard]
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
