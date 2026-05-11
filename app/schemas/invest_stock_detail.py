from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
        exposed = {item.lower() for item in [*self.mappedFields, *(m.label for m in self.metrics)]}
        if any(any(token in item for token in blocked) for item in exposed):
            raise ValueError("discussion signal may expose aggregate metrics only")
        return self


class StockDetailHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalQuantity: float
    averageCost: float | None = None
    costBasis: float | None = None
    valueNative: float | None = None
    valueKrw: float | None = None
    pnlKrw: float | None = None
    pnlRate: float | None = None
    includedSources: list[AccountSourceLiteral]
    priceState: PriceStateLiteral = "live"


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
    holding: StockDetailHolding | None = None
    latestAnalysis: StockDetailLatestAnalysis | None = None
    orderbookSupport: StockDetailOrderbookSupport
    orderbook: StockDetailOrderbook | None = None
    capabilities: StockDetailCapabilities
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
        orderbook=CapabilityFlag(supported=False, reason="crypto_deferred"),
    )


def orderbook_support_for_market(
    market: StockDetailMarket,
) -> StockDetailOrderbookSupport:
    if market == "kr":
        return StockDetailOrderbookSupport(supported=True, reason=None)
    if market == "us":
        return StockDetailOrderbookSupport(supported=False, reason="us_unsupported")
    return StockDetailOrderbookSupport(supported=False, reason="crypto_deferred")
