from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PriceStateLiteral = Literal["live", "stale", "missing"]
SourceLiteral = Literal["kis_live"]
OrderSideLiteral = Literal["buy", "sell", "unknown"]
KISUSOrderPreviewSideLiteral = Literal["buy", "sell"]
KISUSOrderPreviewStatusLiteral = Literal["pass", "blocked"]
USHeldActionLiteral = Literal["sell", "trim", "hold", "add", "watch"]
JournalStatusLiteral = Literal[
    "active", "draft", "missing", "stale", "inactive", "paper"
]


class ScreenedUSNewBuyCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    name: str | None = None
    market: str | None = None
    instrument_type: str | None = Field(default=None, alias="instrumentType")
    price: float | None = None
    change_rate: float | None = Field(default=None, alias="changeRate")
    volume: float | None = None
    trade_amount_24h: float | None = Field(default=None, alias="tradeAmount24h")
    volume_ratio: float | None = Field(default=None, alias="volumeRatio")
    rsi: float | None = None
    market_cap: float | None = Field(default=None, alias="marketCap")
    per: float | None = None
    pbr: float | None = None
    sector: str | None = None
    score: float | None = None
    data_warnings: list[str] = Field(default_factory=list, alias="dataWarnings")


class USNewBuyCandidateCard(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    name: str | None = None
    label: str = "검토 후보"
    priority_label: str = Field(alias="priorityLabel")
    price_usd: float | None = Field(default=None, alias="priceUsd")
    sizing_basis_usd: float | None = Field(default=None, alias="sizingBasisUsd")
    quantity_estimate: int = Field(default=0, alias="quantityEstimate")
    notional_estimate_usd: float = Field(default=0.0, alias="notionalEstimateUsd")
    sizing_note: str = Field(alias="sizingNote")
    thesis: str
    target_price_usd: float | None = Field(default=None, alias="targetPriceUsd")
    stop_loss_usd: float | None = Field(default=None, alias="stopLossUsd")
    min_hold_days: int = Field(default=14, alias="minHoldDays")
    risk_notes: list[str] = Field(default_factory=list, alias="riskNotes")
    data_warnings: list[str] = Field(default_factory=list, alias="dataWarnings")


class USNewBuyCandidateCards(list[USNewBuyCandidateCard]):
    """List-like candidate-card container carrying aggregate warnings."""

    def __init__(
        self,
        cards: list[USNewBuyCandidateCard] | None = None,
        *,
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__(cards or [])
        self.warnings = warnings or []


class USOpenOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    side: OrderSideLiteral = "unknown"
    quantity: float | None = None
    remaining_qty: float | None = Field(default=None, alias="remainingQty")
    pending_qty: float = Field(default=0.0, alias="pendingQty")
    order_id: str | None = Field(default=None, alias="orderId")


class USHolding(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    display_name: str = Field(alias="displayName")
    quantity: float
    average_cost_usd: float | None = Field(default=None, alias="averageCostUsd")
    cost_basis_usd: float | None = Field(default=None, alias="costBasisUsd")
    last_price_usd: float | None = Field(default=None, alias="lastPriceUsd")
    value_usd: float | None = Field(default=None, alias="valueUsd")
    pnl_usd: float | None = Field(default=None, alias="pnlUsd")
    pnl_rate: float | None = Field(default=None, alias="pnlRate")
    price_state: PriceStateLiteral = Field(default="live", alias="priceState")
    source_of_truth: bool = Field(default=True, alias="sourceOfTruth")
    is_tradeable: bool = Field(default=True, alias="isTradeable")
    manual_only: bool = Field(default=False, alias="manualOnly")
    sellable_qty: float = Field(default=0.0, alias="sellableQty")
    pending_qty: float = Field(default=0.0, alias="pendingQty")


class KISUSAccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    captured_at: datetime = Field(alias="capturedAt")
    source: SourceLiteral = "kis_live"
    source_of_truth: bool = Field(default=True, alias="sourceOfTruth")
    is_tradeable: bool = Field(default=True, alias="isTradeable")
    manual_only: bool = Field(default=False, alias="manualOnly")
    usd_cash: float | None = Field(default=None, alias="usdCash")
    usd_buying_power: float | None = Field(default=None, alias="usdBuyingPower")
    holdings: list[USHolding] = Field(default_factory=list)
    open_orders: list[USOpenOrder] = Field(default_factory=list, alias="openOrders")
    warnings: list[str] = Field(default_factory=list)


class KISUSOrderPreviewLadderRung(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    quantity: float | None = None
    limit_price_usd: float = Field(alias="limitPriceUsd")


class KISUSOrderPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    side: KISUSOrderPreviewSideLiteral
    order_type: Literal["limit"] = Field(default="limit", alias="orderType")
    quantity: float
    limit_price_usd: float = Field(alias="limitPriceUsd")
    reference_price_usd: float | None = Field(default=None, alias="referencePriceUsd")
    best_bid_usd: float | None = Field(default=None, alias="bestBidUsd")
    atr_usd: float | None = Field(default=None, alias="atrUsd")
    ladder_rungs: list[KISUSOrderPreviewLadderRung] = Field(
        default_factory=list, alias="ladderRungs"
    )
    thesis: str | None = None
    strategy: str | None = None
    target_price_usd: float | None = Field(default=None, alias="targetPriceUsd")
    stop_loss_usd: float | None = Field(default=None, alias="stopLossUsd")
    min_hold_days: int | None = Field(default=None, alias="minHoldDays")


class KISUSOrderPreviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    side: KISUSOrderPreviewSideLiteral
    order_type: Literal["limit"] = Field(default="limit", alias="orderType")
    quantity: float
    limit_price_usd: float = Field(alias="limitPriceUsd")
    notional_usd: float = Field(alias="notionalUsd")
    status: KISUSOrderPreviewStatusLiteral
    submit_enabled: bool = Field(default=False, alias="submitEnabled")
    blocked_reasons: list[str] = Field(default_factory=list, alias="blockedReasons")
    warnings: list[str] = Field(default_factory=list)
    check_details: dict[str, object] = Field(default_factory=dict, alias="checkDetails")


class KISUSOrderSubmitDisabledError(RuntimeError):
    """Raised when this preview-only flow is asked to submit a live order."""


class USHeldPositionActionCard(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    symbol: str
    display_name: str = Field(alias="displayName")
    action: USHeldActionLiteral
    suggested_trim_pct: int | None = Field(default=None, alias="suggestedTrimPct")
    executable_qty: float = Field(default=0.0, alias="executableQty")
    quantity: float
    sellable_qty: float = Field(alias="sellableQty")
    pending_sell_qty: float = Field(default=0.0, alias="pendingSellQty")
    pending_buy_qty: float = Field(default=0.0, alias="pendingBuyQty")
    pnl_rate: float | None = Field(default=None, alias="pnlRate")
    pnl_usd: float | None = Field(default=None, alias="pnlUsd")
    last_price_usd: float | None = Field(default=None, alias="lastPriceUsd")
    average_cost_usd: float | None = Field(default=None, alias="averageCostUsd")
    target_price_usd: float | None = Field(default=None, alias="targetPriceUsd")
    stop_loss_usd: float | None = Field(default=None, alias="stopLossUsd")
    hold_until: datetime | None = Field(default=None, alias="holdUntil")
    journal_status: JournalStatusLiteral = Field(
        default="missing", alias="journalStatus"
    )
    thesis: str | None = None
    reason_codes: list[str] = Field(default_factory=list, alias="reasonCodes")
    missing_context_codes: list[str] = Field(
        default_factory=list, alias="missingContextCodes"
    )
    warnings: list[str] = Field(default_factory=list)


class USHeldPositionActionCards(list[USHeldPositionActionCard]):
    """List-like held-position action container carrying aggregate warnings."""

    def __init__(
        self,
        cards: list[USHeldPositionActionCard] | None = None,
        *,
        warnings: list[str] | None = None,
    ) -> None:
        super().__init__(cards or [])
        self.warnings = warnings or []
