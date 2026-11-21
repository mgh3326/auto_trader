"""DTOs and shared helpers for the Upbit public read-model cache."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

UpbitBlockState = Literal["fresh", "stale", "unavailable", "missing"]
UpbitSource = Literal[
    "upbit_ticker",
    "upbit_orderbook",
    "upbit_trades",
    "upbit_candles",
    "upbit_market_warnings",
    "upbit_balances",
    "local_pending_orders",
]

TICKER_TTL_SECONDS = 5
TICKER_STALE_TOLERANCE_SECONDS = 60
ORDERBOOK_TTL_SECONDS = 3
ORDERBOOK_STALE_TOLERANCE_SECONDS = 30
TRADES_TTL_SECONDS = 5
TRADES_STALE_TOLERANCE_SECONDS = 30
WARNINGS_TTL_SECONDS = 300


def _now_utc() -> datetime:
    return datetime.now(UTC)


class UpbitBlockMeta(BaseModel):
    """Per-block freshness/error envelope. Mirrors CryptoSourceState shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: UpbitSource
    state: UpbitBlockState
    label: str
    fetchedAt: datetime | None = None
    cachedAt: datetime | None = None
    ttlSeconds: int | None = None
    errorReason: str | None = None


class UpbitTickerBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: UpbitBlockMeta
    tickers: dict[str, dict[str, Any]] = Field(default_factory=dict)


class UpbitOrderbookBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: UpbitBlockMeta
    orderbooks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    spreadsPct: dict[str, float | None] = Field(default_factory=dict)


class UpbitTradesBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: UpbitBlockMeta
    trades: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)


class UpbitCandlesBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: UpbitBlockMeta
    market: str
    period: Literal["day", "week", "month"]
    rows: list[dict[str, Any]] = Field(default_factory=list)


class UpbitMarketWarningEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    market: str
    #: Legacy 유의 projection (hk 6026): CAUTION only for the ``warning`` state,
    #: NONE for ``none``/``caution:*``, UNKNOWN for ``unknown``/``stale``.
    warning: Literal["NONE", "CAUTION", "UNKNOWN"] = "UNKNOWN"
    #: Closed state: ``none`` | ``warning`` | ``caution:<TYPE,...>`` |
    #: ``unknown`` | ``stale``.
    state: str = "unknown"
    caution_types: list[str] = Field(default_factory=list)
    observed_at: datetime | None = None
    event: dict[str, Any] | None = None


class UpbitMarketWarningsBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")
    meta: UpbitBlockMeta
    entries: dict[str, UpbitMarketWarningEntry] = Field(default_factory=dict)


class UpbitPublicSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asOf: datetime
    ticker: UpbitTickerBlock
    orderbook: UpbitOrderbookBlock
    trades: UpbitTradesBlock | None = None
    marketWarnings: UpbitMarketWarningsBlock
    sources: list[UpbitBlockMeta] = Field(default_factory=list)


def to_crypto_source_state(meta: UpbitBlockMeta):
    """Map read-model block metadata into invest crypto source metadata."""
    from app.schemas.invest_crypto import CryptoSourceState

    return CryptoSourceState(
        source=meta.source,
        state="supported" if meta.state in {"fresh", "stale"} else "unavailable",
        label=meta.label,
        fetchedAt=meta.fetchedAt,
    )
