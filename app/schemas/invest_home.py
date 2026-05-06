from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

AccountKindLiteral = Literal["live", "manual", "paper"]
AccountSourceLiteral = Literal[
    "kis",
    "upbit",
    "toss_manual",
    "pension_manual",
    "isa_manual",
    "kis_mock",
    "kiwoom_mock",
    "alpaca_paper",
    "db_simulated",
]
MarketLiteral = Literal["KR", "US", "CRYPTO"]
AssetTypeLiteral = Literal["equity", "etf", "crypto", "fund", "other"]
CurrencyLiteral = Literal["KRW", "USD"]
AssetCategoryLiteral = Literal["kr_stock", "us_stock", "crypto"]
PriceStateLiteral = Literal["live", "missing", "stale"]


class CashAmounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    krw: float | None = None
    usd: float | None = None


class Account(BaseModel):
    model_config = ConfigDict(extra="forbid")
    accountId: str
    displayName: str
    source: AccountSourceLiteral
    accountKind: AccountKindLiteral
    includedInHome: bool
    valueKrw: float
    costBasisKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    cashBalances: CashAmounts = Field(default_factory=CashAmounts)
    buyingPower: CashAmounts = Field(default_factory=CashAmounts)


class Holding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdingId: str
    accountId: str
    source: AccountSourceLiteral
    accountKind: AccountKindLiteral
    symbol: str
    market: MarketLiteral
    assetType: AssetTypeLiteral
    assetCategory: AssetCategoryLiteral
    displayName: str
    quantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    currency: CurrencyLiteral
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    priceState: PriceStateLiteral = "live"


class GroupedSourceBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdingId: str
    accountId: str
    source: AccountSourceLiteral
    quantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None


class GroupedHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    groupId: str
    symbol: str
    market: MarketLiteral
    assetType: AssetTypeLiteral
    assetCategory: AssetCategoryLiteral
    displayName: str
    currency: CurrencyLiteral
    totalQuantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    priceState: PriceStateLiteral = "live"
    includedSources: list[AccountSourceLiteral]
    sourceBreakdown: list[GroupedSourceBreakdown]


class HomeSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    includedSources: list[AccountSourceLiteral]
    excludedSources: list[AccountSourceLiteral]
    totalValueKrw: float
    costBasisKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None


class InvestHomeWarning(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: AccountSourceLiteral
    message: str


class InvestHomeHiddenCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upbitInactive: int = 0
    upbitDust: int = 0


class InvestHomeResponseMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    warnings: list[InvestHomeWarning] = Field(default_factory=list)
    hiddenCounts: InvestHomeHiddenCounts = Field(default_factory=InvestHomeHiddenCounts)
    hiddenHoldings: list[Holding] = Field(default_factory=list)


class InvestHomeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    homeSummary: HomeSummary
    accounts: list[Account]
    holdings: list[Holding]
    groupedHoldings: list[GroupedHolding]
    meta: InvestHomeResponseMeta = Field(default_factory=InvestHomeResponseMeta)
