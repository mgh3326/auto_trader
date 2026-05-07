"""ROB-141 — /invest/api/account-panel response schema."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invest_home import (
    Account,
    GroupedHolding,
    HomeSummary,
    InvestHomeWarning,
)
from app.services.invest_view_model.account_visual import AccountSourceVisual


class WatchSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: Literal["kr", "us", "crypto"]
    displayName: str
    note: str | None = None


class AccountPanelMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[InvestHomeWarning] = Field(default_factory=list)
    watchlistAvailable: bool = True


class AccountPanelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    homeSummary: HomeSummary
    accounts: list[Account]
    groupedHoldings: list[GroupedHolding]
    watchSymbols: list[WatchSymbol]
    sourceVisuals: list[AccountSourceVisual]
    meta: AccountPanelMeta
