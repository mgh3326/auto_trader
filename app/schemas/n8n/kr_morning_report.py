# app/schemas/n8n/kr_morning_report.py
"""Schemas for the n8n KR morning report endpoint."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

__all__ = [
    "N8nKrPosition",
    "N8nKrHoldingsAccount",
    "N8nKrHoldings",
    "N8nKrCashBalance",
    "N8nKrScreenResult",
    "N8nKrScreening",
    "N8nKrMorningReportResponse",
]


class N8nKrPosition(BaseModel):
    symbol: str
    name: str
    quantity: float = 0
    avg_price: float = 0
    current_price: float | None = None
    eval_krw: float | None = None
    pnl_pct: float | None = None
    pnl_fmt: str | None = None
    eval_fmt: str | None = None
    account: str | None = None


class N8nKrHoldingsAccount(BaseModel):
    total_count: int = 0
    total_eval_krw: float = 0
    total_eval_fmt: str = "0"
    total_pnl_pct: float | None = None
    total_pnl_fmt: str | None = None
    positions: list[N8nKrPosition] = Field(default_factory=list)


class N8nKrHoldings(BaseModel):
    kis: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)
    toss: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)
    combined: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)


class N8nKrCashBalance(BaseModel):
    kis_krw: float = 0
    kis_krw_fmt: str = "0"
    toss_krw: float | None = None
    toss_krw_fmt: str = "수동 관리"
    total_krw: float = 0
    total_krw_fmt: str = "0"


class N8nKrScreenResult(BaseModel):
    symbol: str
    name: str
    current_price: float | None = None
    rsi: float | None = None
    change_pct: float | None = None
    volume_ratio: float | None = None
    market_cap_fmt: str | None = None
    signal: str | None = None
    sector: str | None = None


class N8nKrScreening(BaseModel):
    total_scanned: int = 0
    top_n: int = 0
    strategy: str | None = None
    results: list[N8nKrScreenResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class N8nKrMorningReportResponse(BaseModel):
    success: bool
    as_of: str
    date_fmt: str
    holdings: N8nKrHoldings = Field(default_factory=N8nKrHoldings)
    cash_balance: N8nKrCashBalance = Field(default_factory=N8nKrCashBalance)
    screening: N8nKrScreening = Field(default_factory=N8nKrScreening)
    pending_orders: dict[str, Any] = Field(default_factory=dict)
    brief_text: str = ""
    errors: list[dict[str, str]] = Field(default_factory=list)
