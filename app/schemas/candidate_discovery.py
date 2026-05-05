"""ROB-117 — Candidate discovery DTOs."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CandidateScreenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr", "kospi", "kosdaq", "konex", "all", "us", "crypto"] = "crypto"
    asset_type: Literal["stock", "etf", "etn"] | None = None
    strategy: Literal["oversold", "momentum", "high_volume"] | None = None
    sort_by: Literal[
        "volume", "trade_amount", "market_cap", "change_rate", "dividend_yield", "rsi"
    ] | None = None
    sort_order: Literal["asc", "desc"] = "desc"

    min_market_cap: float | None = None
    max_per: float | None = None
    max_pbr: float | None = None
    min_dividend_yield: float | None = None
    max_rsi: float | None = None
    adv_krw_min: int | None = None
    market_cap_min_krw: int | None = None
    market_cap_max_krw: int | None = None
    exclude_sectors: list[str] | None = None
    instrument_types: list[str] | None = None

    krw_only: bool = False
    exclude_warnings: bool = False
    limit: int = Field(default=50, ge=1, le=100)


class ScreenedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    name: str | None = None
    market: str | None = None
    instrument_type: str | None = None
    price: float | None = None
    change_rate: float | None = None
    volume: float | None = None
    trade_amount_24h: float | None = None
    volume_ratio: float | None = None
    rsi: float | None = None
    market_cap: float | None = None
    per: float | None = None
    pbr: float | None = None
    sector: str | None = None

    is_held: bool = False
    held_quantity: float | None = None
    latest_research_session_id: int | None = None
    research_status: Literal["new", "watch", "exclude"] | None = None

    data_warnings: list[str] = Field(default_factory=list)


class CandidateScreenResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generated_at: str
    market: str
    strategy: str | None
    sort_by: str | None
    total: int
    candidates: list[ScreenedCandidate]
    warnings: list[str] = Field(default_factory=list)
    rsi_enrichment_attempted: int = 0
    rsi_enrichment_succeeded: int = 0
