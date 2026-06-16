from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

InvestorFlowDataState = Literal["empty", "missing", "stale", "fresh", "partial"]


class InvestorFlowItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: Literal["kr"] = "kr"
    dataState: Literal["missing", "stale", "fresh"]
    snapshotDate: date | None = None
    collectedAt: datetime | None = None
    source: str | None = None

    foreignNet: int | None = None
    institutionNet: int | None = None
    individualNet: int | None = None

    # ROB-586: Promote foreign ownership levels
    foreignHoldingShares: int | None = None
    foreignHoldingRate: float | None = None

    # ROB-586: Promote discussion sentiment (proxy via ranking)
    discussionSentimentRank: int | None = None

    foreignNetBuyRank: int | None = None
    foreignNetSellRank: int | None = None
    institutionNetBuyRank: int | None = None
    institutionNetSellRank: int | None = None

    doubleBuy: bool = False
    doubleSell: bool = False

    foreignConsecutiveBuyDays: int | None = None
    foreignConsecutiveSellDays: int | None = None
    institutionConsecutiveBuyDays: int | None = None
    institutionConsecutiveSellDays: int | None = None
    individualConsecutiveBuyDays: int | None = None
    individualConsecutiveSellDays: int | None = None


class InvestorFlowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr"] = "kr"
    asOf: date
    source: str | None = None
    dataState: InvestorFlowDataState
    items: list[InvestorFlowItem] = Field(default_factory=list)
