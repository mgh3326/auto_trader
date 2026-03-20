from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class BacktestPairSummary(BaseModel):
    pair: str = Field(min_length=1)
    total_trades: int = Field(ge=0)
    profit_factor: Decimal | None = None
    max_drawdown: Decimal | None = None
    total_return: Decimal | None = None


class BacktestRunSummary(BaseModel):
    run_id: str = Field(min_length=1)
    strategy_name: str = Field(min_length=1)
    strategy_version: str | None = None
    exchange: str = "binance"
    market: str = "spot"
    timeframe: str = Field(min_length=1)
    timerange: str | None = None
    runner: str = Field(min_length=1)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_trades: int = Field(ge=0)
    profit_factor: Decimal = Field(default=Decimal("0"))
    max_drawdown: Decimal = Field(default=Decimal("0"))
    win_rate: Decimal | None = None
    expectancy: Decimal | None = None
    total_return: Decimal | None = None
    artifact_path: str | None = None
    artifact_hash: str | None = None
    pairs: list[BacktestPairSummary] = Field(default_factory=list)
    raw_payload: dict | None = None

    model_config = ConfigDict(extra="ignore")
