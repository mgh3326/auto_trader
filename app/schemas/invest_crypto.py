"""ROB-226 — read-only crypto dashboard DTOs for /invest/api/crypto/*.

These models intentionally expose public market/read-model state only. Execution,
watch/order-intent, and broker mutation controls stay out of this contract.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

CryptoCapabilityState = Literal[
    "supported",
    "unavailable",
    "reference_only",
    "external_gap",
    "deferred",
    "read_only_mvp",
]
CryptoRiskBadgeKind = Literal[
    "thin_orderbook",
    "held",
    "pending_order",
    "data_unavailable",
    "high_volatility",
    "low_liquidity",
    "candidate_watch",
    "momentum_candidate",
]
CryptoRiskLevel = Literal["low", "medium", "high", "unknown"]
CryptoCandidateReasonKind = Literal[
    "momentum",
    "liquidity",
    "spread",
    "watched",
    "held",
    "pending_order",
    "data_quality",
]
CryptoPendingOrderSide = Literal["buy", "sell"] | str


class CryptoSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    state: CryptoCapabilityState
    label: str
    fetchedAt: datetime | None = None


class CryptoCapabilityFlag(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: CryptoCapabilityState
    reason: str | None = None


class CryptoDashboardCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(state="supported")
    )
    candles: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(state="supported")
    )
    orderbook: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(state="supported")
    )
    recentTrades: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(
            state="external_gap", reason="upbit_public_dashboard_mvp"
        )
    )
    projectInfo: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(
            state="reference_only", reason="external_reference_only"
        )
    )
    liveStreaming: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(state="deferred")
    )
    execution: CryptoCapabilityFlag = Field(
        default_factory=lambda: CryptoCapabilityFlag(state="read_only_mvp")
    )


class CryptoRiskBadge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: CryptoRiskBadgeKind
    label: str
    severity: Literal["info", "warning", "danger"] = "info"


class CryptoRiskSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: CryptoRiskLevel
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(default_factory=list)


class CryptoCandidateInsight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    baseSymbol: str
    displayName: str
    rank: int = Field(ge=1)
    score: int = Field(ge=0, le=100)
    reasons: list[CryptoCandidateReasonKind] = Field(default_factory=list)
    summary: str
    isHeld: bool = False
    isWatched: bool = False
    hasPendingOrder: bool = False
    riskLevel: CryptoRiskLevel


class CryptoMarketCard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    baseSymbol: str
    displayName: str
    priceKrw: float | None = None
    changeRate24h: float | None = None
    changeAmount24h: float | None = None
    accTradePrice24h: float | None = None
    volume24h: float | None = None
    orderbookSpreadPct: float | None = None
    isHeld: bool = False
    isWatched: bool = False
    badges: list[CryptoRiskBadge] = Field(default_factory=list)
    risk: CryptoRiskSummary | None = None


class CryptoHoldingSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heldCount: int = Field(ge=0)
    symbols: list[str] = Field(default_factory=list)
    source: Literal["invest_home_read_model"] = "invest_home_read_model"


class CryptoPendingOrderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    orderId: str | None = None
    symbol: str
    baseSymbol: str | None = None
    side: CryptoPendingOrderSide
    orderType: str | None = None
    price: float | None = None
    quantity: float
    filledQuantity: float = 0
    status: str
    orderedAt: datetime | None = None
    updatedAt: datetime | None = None
    source: Literal["pending_orders"] = "pending_orders"


class CryptoPendingOrdersSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[CryptoPendingOrderItem] = Field(default_factory=list)
    emptyState: Literal["no_pending_orders"] | None = None
    source: Literal["pending_orders"] = "pending_orders"

    @field_validator("emptyState")
    @classmethod
    def empty_state_matches_items(cls, value, info):
        items = info.data.get("items") or []
        if items and value is not None:
            raise ValueError("emptyState must be null when pending orders are present")
        if not items and value != "no_pending_orders":
            raise ValueError(
                "emptyState must be no_pending_orders when no pending orders exist"
            )
        return value


class CryptoInsightsSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    badges: list[CryptoRiskBadge] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    candidates: list[CryptoCandidateInsight] = Field(default_factory=list)


class CryptoDashboardMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    warnings: list[str] = Field(default_factory=list)
    sources: list[CryptoSourceState] = Field(default_factory=list)


class CryptoDashboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asOf: datetime
    market: Literal["crypto"] = "crypto"
    baseCurrency: Literal["KRW"] = "KRW"
    cards: list[CryptoMarketCard]
    holdings: CryptoHoldingSummary | None = None
    pendingOrders: CryptoPendingOrdersSummary | None = None
    insights: CryptoInsightsSummary
    capabilities: CryptoDashboardCapabilities = Field(
        default_factory=CryptoDashboardCapabilities
    )
    meta: CryptoDashboardMeta
