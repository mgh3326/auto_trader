from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class MomentumEventItem(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    snapshot_at: dt.datetime = Field(alias="snapshotAt")
    trading_date: dt.date = Field(alias="tradingDate")
    source: str
    surface: str
    market: str
    trade_type: str | None = Field(default=None, alias="tradeType")
    market_type: str | None = Field(default=None, alias="marketType")
    order_type: str = Field(alias="orderType")
    rank: int
    symbol: str
    name: str | None = None
    price: Decimal | None = None
    change_amount: Decimal | None = Field(default=None, alias="changeAmount")
    change_rate: Decimal | None = Field(default=None, alias="changeRate")
    volume: int | None = None
    trade_value: Decimal | None = Field(default=None, alias="tradeValue")
    market_cap: Decimal | None = Field(default=None, alias="marketCap")


class MomentumEventsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market: str
    data_state: str = Field(alias="dataState")
    empty_reason: str | None = Field(default=None, alias="emptyReason")
    items: list[MomentumEventItem]


class ThemeEventItem(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    snapshot_at: dt.datetime = Field(alias="snapshotAt")
    trading_date: dt.date = Field(alias="tradingDate")
    source: str
    surface: str
    market: str
    event_kind: Literal["theme", "upjong"] = Field(alias="eventKind")
    source_event_key: str = Field(alias="sourceEventKey")
    naver_theme_no: str | None = Field(default=None, alias="naverThemeNo")
    naver_upjong_code: str | None = Field(default=None, alias="naverUpjongCode")
    name: str
    sort_type: str = Field(alias="sortType")
    rank: int | None = None
    market_type: str | None = Field(default=None, alias="marketType")
    change_rate: Decimal | None = Field(default=None, alias="changeRate")
    trade_value: Decimal | None = Field(default=None, alias="tradeValue")
    market_cap: Decimal | None = Field(default=None, alias="marketCap")
    stock_count: int | None = Field(default=None, alias="stockCount")
    leader_symbols: list[dict[str, Any]] = Field(
        default_factory=list, alias="leaderSymbols"
    )


class ThemeEventsResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market: str
    data_state: str = Field(alias="dataState")
    empty_reason: str | None = Field(default=None, alias="emptyReason")
    items: list[ThemeEventItem]


class MomentumCandidateItem(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    symbol: str
    name: str | None = None
    score: float
    latest_snapshot_at: dt.datetime = Field(alias="latestSnapshotAt")
    trading_date: dt.date = Field(alias="tradingDate")
    price: Decimal | None = None
    change_rate: Decimal | None = Field(default=None, alias="changeRate")
    surface_count: int = Field(alias="surfaceCount")
    venue_count: int = Field(alias="venueCount")
    rank_delta: int | None = Field(default=None, alias="rankDelta")
    signals: list[dict[str, Any]]
    theme_names: list[str] = Field(default_factory=list, alias="themeNames")
    reason_codes: list[str] = Field(default_factory=list, alias="reasonCodes")


class MomentumCandidatesResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market: str
    data_state: str = Field(alias="dataState")
    empty_reason: str | None = Field(default=None, alias="emptyReason")
    items: list[MomentumCandidateItem]


class MomentumCoverageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    market: str
    as_of: dt.date = Field(alias="asOf")
    momentum_events: int = Field(alias="momentumEvents")
    theme_events: int = Field(alias="themeEvents")
    last_momentum_snapshot_at: dt.datetime | None = Field(
        default=None, alias="lastMomentumSnapshotAt"
    )
    last_theme_snapshot_at: dt.datetime | None = Field(
        default=None, alias="lastThemeSnapshotAt"
    )
    data_state: str = Field(alias="dataState")
    empty_reason: str | None = Field(default=None, alias="emptyReason")
