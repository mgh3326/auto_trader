from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.invest_crypto import CryptoPendingOrdersSummary, CryptoSourceState
from app.schemas.invest_feed_news import FeedNewsResponse, NewsMarket
from app.schemas.invest_home import (
    AccountSourceLiteral,
    AssetCategoryLiteral,
    AssetTypeLiteral,
    CurrencyLiteral,
    PriceStateLiteral,
)

type StockDetailMarket = NewsMarket
OrderbookUnsupportedReason = Literal[
    "us_unsupported",
    "crypto_deferred",
    "kr_unavailable",
    "provider_unavailable",
]
CapabilityUnsupportedReason = Literal[
    "read_only_mvp",
    "out_of_mvp_scope",
    "us_unsupported",
    "crypto_deferred",
    "unsupported_period",
]
ValuationFreshness = Literal["ok", "stale", "unsupported", "error"]
ScreenerSnapshotFreshness = Literal["fresh", "stale", "missing"]
NaverPocStatus = Literal["fixture_backed_poc", "no_go"]
NaverEndpointStatus = Literal[
    "verified_200",
    "verified_200_signal_only",
    "page_candidate",
    "needs_auth_or_contract_check",
    "unsupported",
    "error",
]
OrderSide = Literal["buy", "sell"]
AnalysisDecision = Literal["buy", "hold", "sell"]
DiscussionSignalStatus = Literal["fixture_backed_poc", "no_go_pending_review"]
DiscussionSignalMomentum = Literal["rising", "flat", "falling", "unknown"]
DiscussionSignalFreshness = Literal["fixture", "stale", "missing"]
FxSensitivityStatus = Literal[
    "available",
    "not_applicable",
    "missing_holding",
    "missing_native_value",
    "missing_fx_rate",
]
FxSensitivityBasis = Literal["portfolio_value", "fallback_quote", "not_applicable"]


class CapabilityFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: bool
    reason: str | None = None

    @model_validator(mode="after")
    def require_reason_when_unsupported(self) -> CapabilityFlag:
        if not self.supported and not self.reason:
            raise ValueError("unsupported capabilities must include a reason")
        if self.supported and self.reason is not None:
            raise ValueError("supported capabilities must not include a reason")
        return self


class CandleCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    supported: bool = True
    intradaySupported: bool = True


class StockDetailCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candles: CandleCapability = Field(default_factory=CandleCapability)
    orderbook: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(supported=True, reason=None)
    )
    news: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(supported=True, reason=None)
    )
    orders: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(supported=True, reason=None)
    )
    liveStreaming: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(
            supported=False, reason="out_of_mvp_scope"
        )
    )
    execution: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(supported=False, reason="read_only_mvp")
    )
    options: CapabilityFlag = Field(
        default_factory=lambda: CapabilityFlag(
            supported=False, reason="out_of_mvp_scope"
        )
    )

    @model_validator(mode="after")
    def enforce_read_only_contract(self) -> StockDetailCapabilities:
        if self.execution.supported:
            raise ValueError("stock-detail MVP is read-only: execution is unsupported")
        if self.options.supported:
            raise ValueError("stock-detail MVP does not support options")
        return self


class StockDetailQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float | None = None
    previousClose: float | None = None
    changeAmount: float | None = None
    changeRate: float | None = None
    asOf: datetime | None = None
    priceState: PriceStateLiteral = "missing"


class StockDetailScreenerSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshotDate: str
    consecutiveUpDays: int | None = None
    weekChangeRate: float | None = None
    dailyVolume: float | None = None
    closesWindow: list[float] = Field(default_factory=list)
    source: str | None = None
    freshness: ScreenerSnapshotFreshness = "missing"


class StockDetailValuation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per: float | None = None
    pbr: float | None = None
    roe: float | None = None
    dividendYield: float | None = None
    high52w: float | None = None
    low52w: float | None = None
    marketCap: float | None = None
    source: str
    asOf: datetime | None = None
    freshness: ValuationFreshness = "ok"


class StockDetailNaverEndpointProbe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    url: str
    status: NaverEndpointStatus
    payloadFields: list[str] = Field(default_factory=list)
    mappedFields: list[str] = Field(default_factory=list)
    risk: str


class StockDetailNaverEnrichment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["naver_stock_detail_poc"] = "naver_stock_detail_poc"
    market: StockDetailMarket
    symbol: str
    naverCode: str
    pageUrl: str
    status: NaverPocStatus = "fixture_backed_poc"
    liveFetchEnabled: bool = False
    endpoints: list[StockDetailNaverEndpointProbe] = Field(default_factory=list)
    usefulFields: list[str] = Field(default_factory=list)
    noGoFields: list[str] = Field(default_factory=list)
    docsPath: str


class StockDetailDiscussionSignalMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: int | float | str | None = None
    unit: str | None = None


class StockDetailDiscussionSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["naver_discussion_signal_poc"] = "naver_discussion_signal_poc"
    market: StockDetailMarket
    symbol: str
    naverCode: str
    status: DiscussionSignalStatus = "no_go_pending_review"
    liveFetchEnabled: bool = False
    freshness: DiscussionSignalFreshness = "fixture"
    observedAt: datetime | None = None
    windowLabel: str
    activityRank: int | None = None
    postCount: int | None = None
    commentCount: int | None = None
    reactionCount: int | None = None
    momentum: DiscussionSignalMomentum = "unknown"
    metrics: list[StockDetailDiscussionSignalMetric] = Field(default_factory=list)
    mappedFields: list[str] = Field(default_factory=list)
    noGoFields: list[str] = Field(default_factory=list)
    risk: str
    docsPath: str

    @model_validator(mode="after")
    def enforce_aggregate_only_contract(self) -> StockDetailDiscussionSignal:
        if self.liveFetchEnabled:
            raise ValueError("ROB-199 discussion PoC must not enable live fetching")
        blocked = {
            "post_text",
            "post_title",
            "comment_text",
            "author",
            "user_id",
            "nickname",
            "body",
            "title",
        }
        exposed = {
            item.lower()
            for item in [*self.mappedFields, *(m.label for m in self.metrics)]
        }
        if any(any(token in item for token in blocked) for item in exposed):
            raise ValueError("discussion signal may expose aggregate metrics only")
        return self


InvestorFlowDetailState = Literal["fresh", "stale", "missing"]


class StockDetailInvestorFlowDailyRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshotDate: str
    collectedAt: datetime | None = None
    source: str | None = None
    close: float | None = None
    changeRate: float | None = None
    volume: int | None = None
    foreignNet: int | None = None
    foreignHoldingShares: int | None = None
    foreignHoldingRate: float | None = None
    institutionNet: int | None = None
    individualNet: int | None = None
    doubleBuy: bool = False
    doubleSell: bool = False


class StockDetailInvestorFlowPeriodSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    windowDays: int
    rowCount: int
    foreignNetTotal: int | None = None
    institutionNetTotal: int | None = None
    individualNetTotal: int | None = None
    foreignBuyDays: int = 0
    foreignSellDays: int = 0
    foreignFlatDays: int = 0
    foreignNetToVolumeRatio: float | None = None
    foreignHoldingSharesChange: int | None = None
    foreignHoldingRateChange: float | None = None
    unavailableLabels: list[str] = Field(default_factory=list)


class StockDetailInvestorFlowBuyerDecomposition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshotDate: str
    label: str
    leadingBuyer: Literal["foreign", "institution", "individual", "mixed", "unknown"]
    foreignNet: int | None = None
    institutionNet: int | None = None
    individualNet: int | None = None
    note: str


class StockDetailInvestorFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["investor_flow_snapshots"] = "investor_flow_snapshots"
    market: Literal["kr"] = "kr"
    symbol: str
    dataState: InvestorFlowDetailState
    snapshotDate: str | None = None
    collectedAt: datetime | None = None
    snapshotSource: str | None = None
    foreignNet: int | None = None
    institutionNet: int | None = None
    individualNet: int | None = None

    # ROB-586: Promote foreign ownership levels
    foreignHoldingShares: int | None = None
    foreignHoldingRate: float | None = None

    # ROB-586: Promote discussion sentiment (proxy via ranking)
    discussionSentimentRank: int | None = None
    discussionSentimentOverheat: bool = False

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
    dailyRows: list[StockDetailInvestorFlowDailyRow] = Field(default_factory=list)
    periodSummary: StockDetailInvestorFlowPeriodSummary | None = None
    buyerDecomposition: StockDetailInvestorFlowBuyerDecomposition | None = None
    unavailableLabels: list[str] = Field(default_factory=list)
    cautionLabel: str = (
        "투자자별 수급은 지연된 과거 참고 데이터이며 매매 판단을 대신하지 않습니다."
    )

    @model_validator(mode="after")
    def enforce_kr_only(self) -> StockDetailInvestorFlow:
        if self.market != "kr":
            raise ValueError("investor_flow is KR-only in /invest stock detail")
        if self.dataState == "missing" and (
            self.foreignNet is not None
            or self.institutionNet is not None
            or self.individualNet is not None
        ):
            raise ValueError("missing investor_flow must not expose any flow values")
        return self


class StockDetailHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    includedSources: list[AccountSourceLiteral]
    priceState: PriceStateLiteral = "live"


class StockDetailFxScenario(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rateMovePct: float
    estimatedKrwImpact: float | None = None
    estimatedValueKrw: float | None = None
    label: str


class StockDetailFxSensitivity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["stock_detail_fx_sensitivity"] = "stock_detail_fx_sensitivity"
    status: FxSensitivityStatus
    currencyPair: Literal["USD/KRW"] | None = None
    baseFxRate: float | None = None
    holdingValueNative: float | None = None
    holdingValueKrw: float | None = None
    basis: FxSensitivityBasis = "not_applicable"
    scenarios: list[StockDetailFxScenario] = Field(default_factory=list)
    caution: str

    @model_validator(mode="after")
    def enforce_status_shape(self) -> StockDetailFxSensitivity:
        if self.status == "available":
            if self.currencyPair != "USD/KRW" or self.baseFxRate is None:
                raise ValueError("available FX sensitivity requires USD/KRW rate")
            if self.holdingValueNative is None or self.holdingValueNative <= 0:
                raise ValueError(
                    "available FX sensitivity requires positive native value"
                )
            if not self.scenarios:
                raise ValueError("available FX sensitivity requires scenarios")
        elif self.scenarios:
            raise ValueError("unavailable FX sensitivity must not expose scenarios")
        return self


class StockDetailLatestAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int
    modelName: str | None = None
    decision: AnalysisDecision | None = None
    confidence: float | None = None
    appropriateBuyRange: tuple[float | None, float | None] | None = None
    appropriateSellRange: tuple[float | None, float | None] | None = None
    reasonsTop3: list[str] = Field(default_factory=list, max_length=3)
    createdAt: datetime | None = None


class StockDetailOrderbookLevel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price: float
    quantity: float


class StockDetailOrderbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asOf: datetime | None = None
    asks: list[StockDetailOrderbookLevel] = Field(default_factory=list)
    bids: list[StockDetailOrderbookLevel] = Field(default_factory=list)


class StockDetailOrderbookSupport(CapabilityFlag):
    reason: OrderbookUnsupportedReason | None = None


CryptoRecentTradesState = Literal["supported", "empty", "unavailable"]
CryptoPreOrderCheckState = Literal["ok", "warning", "danger", "unavailable", "info"]


class CryptoDetailProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    baseSymbol: str
    displayNameKo: str | None = None
    displayNameEn: str | None = None
    quoteCurrency: Literal["KRW"] = "KRW"
    source: str = "upbit_symbol_universe"
    state: Literal["supported", "unavailable"] = "supported"
    asOf: datetime | None = None


class CryptoRecentTradeItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tradeTime: datetime | None = None
    priceKrw: float
    volume: float
    side: str | None = None
    sequentialId: str | int | None = None
    source: Literal["upbit_recent_trades"] = "upbit_recent_trades"


class CryptoRecentTrades(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CryptoRecentTradeItem] = Field(default_factory=list)
    emptyState: Literal["no_recent_trades"] | None = None
    source: Literal["upbit_recent_trades"] = "upbit_recent_trades"
    state: CryptoRecentTradesState
    asOf: datetime | None = None
    warnings: list[str] = Field(default_factory=list)


class CryptoPreOrderCheckItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label: str
    state: CryptoPreOrderCheckState
    detail: str
    source: str
    computedAt: datetime


class CryptoPreOrderChecklist(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["informational_only"] = "informational_only"
    items: list[CryptoPreOrderCheckItem] = Field(default_factory=list)
    disclaimer: str = "참고용 체크리스트입니다. 주문을 생성하거나 승인하지 않습니다."
    sources: list[str] = Field(default_factory=list)


class CryptoDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: CryptoDetailProfile
    recentTrades: CryptoRecentTrades
    pendingOrders: CryptoPendingOrdersSummary
    preOrderChecklist: CryptoPreOrderChecklist
    sources: list[CryptoSourceState] = Field(default_factory=list)


class StockDetailMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    computedAt: datetime
    warnings: list[str] = Field(default_factory=list)


class StockDetailResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: StockDetailMarket
    displayName: str
    exchange: str
    instrumentType: str
    currency: CurrencyLiteral
    assetType: AssetTypeLiteral
    assetCategory: AssetCategoryLiteral
    quote: StockDetailQuote | None = None
    screenerSnapshot: StockDetailScreenerSnapshot | None = None
    valuation: StockDetailValuation | None = None
    naverEnrichment: StockDetailNaverEnrichment | None = None
    discussionSignal: StockDetailDiscussionSignal | None = None
    investorFlow: StockDetailInvestorFlow | None = None
    holding: StockDetailHolding | None = None
    fxSensitivity: StockDetailFxSensitivity | None = None
    latestAnalysis: StockDetailLatestAnalysis | None = None
    orderbookSupport: StockDetailOrderbookSupport
    orderbook: StockDetailOrderbook | None = None
    capabilities: StockDetailCapabilities
    cryptoDetail: CryptoDetail | None = None
    meta: StockDetailMeta

    @model_validator(mode="after")
    def orderbook_matches_support_flag(self) -> StockDetailResponse:
        if self.orderbookSupport.supported and self.orderbook is None:
            raise ValueError(
                "orderbook is required when orderbookSupport.supported=true"
            )
        if not self.orderbookSupport.supported and self.orderbook is not None:
            raise ValueError(
                "orderbook must be null when orderbookSupport.supported=false"
            )
        return self


class StockDetailCandle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None


class StockDetailCandlesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: StockDetailMarket
    period: str
    source: str
    candles: list[StockDetailCandle] = Field(default_factory=list)
    capabilities: CandleCapability = Field(default_factory=CandleCapability)


type StockDetailNewsResponse = FeedNewsResponse


class StockDetailOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orderId: str | None = None
    symbol: str
    market: StockDetailMarket
    side: OrderSide | str
    quantity: float
    price: float | None = None
    filledAt: datetime | None = None
    account: str | None = None
    source: str | None = None


class StockDetailOrdersMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emptyState: Literal["no_filled_orders"] | None = None
    warnings: list[str] = Field(default_factory=list)


class StockDetailOrdersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: StockDetailMarket
    items: list[StockDetailOrder] = Field(default_factory=list)
    nextCursor: str | None = None
    meta: StockDetailOrdersMeta = Field(default_factory=StockDetailOrdersMeta)


def default_capabilities_for_market(
    market: StockDetailMarket,
) -> StockDetailCapabilities:
    if market == "kr":
        return StockDetailCapabilities()
    if market == "us":
        return StockDetailCapabilities(
            orderbook=CapabilityFlag(supported=False, reason="us_unsupported")
        )
    return StockDetailCapabilities(
        candles=CandleCapability(supported=True, intradaySupported=False),
        orderbook=CapabilityFlag(supported=True, reason=None),
    )


def orderbook_support_for_market(
    market: StockDetailMarket,
) -> StockDetailOrderbookSupport:
    if market == "kr":
        return StockDetailOrderbookSupport(supported=True, reason=None)
    if market == "us":
        return StockDetailOrderbookSupport(supported=False, reason="us_unsupported")
    return StockDetailOrderbookSupport(supported=True, reason=None)
