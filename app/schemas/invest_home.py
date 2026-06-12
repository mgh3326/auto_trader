from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

AccountKindLiteral = Literal["live", "manual", "paper"]
AccountSourceLiteral = Literal[
    "kis",
    "upbit",
    "toss_manual",
    "toss_api",
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
    sourceOfTruth: bool = True
    isTradeable: bool = True
    manualOnly: bool = False
    sellableQuantity: float | None = None
    pendingSellQuantity: float = 0.0
    referenceQuantity: float | None = None

    @model_validator(mode="after")
    def apply_source_separation_defaults(self) -> Holding:
        """Keep manual/reference holdings out of tradeable and sellable totals."""

        if self.accountKind == "manual" or self.source in {
            "toss_manual",
            "pension_manual",
            "isa_manual",
        }:
            self.sourceOfTruth = False
            self.isTradeable = False
            self.manualOnly = True
            self.sellableQuantity = 0.0
            self.pendingSellQuantity = 0.0
            self.referenceQuantity = self.quantity
        elif self.referenceQuantity is None:
            self.referenceQuantity = 0.0
        return self


class GroupedSourceBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")
    holdingId: str
    accountId: str
    source: AccountSourceLiteral
    accountKind: AccountKindLiteral
    quantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    sourceOfTruth: bool = True
    isTradeable: bool = True
    manualOnly: bool = False
    sellableQuantity: float | None = None
    pendingSellQuantity: float = 0.0
    referenceQuantity: float | None = None


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
    tradeableQuantity: float = 0.0
    sellableQuantity: float = 0.0
    pendingSellQuantity: float = 0.0
    referenceQuantity: float = 0.0
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
