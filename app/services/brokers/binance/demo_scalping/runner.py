"""ROB-307 PR1 — observe-only scalping runner.

Orchestrates: read-only Demo market data → deterministic signal → risk
envelope (with an injected ledger snapshot) → a single auditable
``ObserveOnlyRecord``. PR1 **never executes**: ``action`` is always
``observe_only``. ``would_enter`` records whether signal + risk *would*
have permitted entry, for downstream PR2 wiring — but no order is placed
and no broker mutation is reachable from here (enforced by the import
guard).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.services.brokers.binance.demo_scalping.contract import (
    LedgerSnapshot,
    MarketConditions,
    Product,
    ScalpingRiskLimits,
    Side,
    evaluate_risk,
)
from app.services.brokers.binance.demo_scalping.market_data import (
    BookTicker,
    data_age_seconds,
    spread_bps,
)
from app.services.brokers.binance.demo_scalping.signal import (
    Candle,
    SignalConfig,
    evaluate_signal,
)

_ACTION_OBSERVE_ONLY = "observe_only"


class MarketDataReader(Protocol):
    async def fetch_klines(
        self, product: Product, symbol: str, *, interval: str = ..., limit: int = ...
    ) -> list[Candle]: ...

    async def fetch_book_ticker(self, product: Product, symbol: str) -> BookTicker: ...


@dataclass(frozen=True)
class ObserveOnlyRecord:
    product: Product
    symbol: str
    has_entry: bool
    side: Side | None
    entry_price: Decimal | None
    tp_price: Decimal | None
    sl_price: Decimal | None
    confidence: Decimal
    signal_reason_codes: tuple[str, ...]
    risk_allowed: bool
    risk_reason_codes: tuple[str, ...]
    would_enter: bool
    spread_bps: Decimal
    data_age_seconds: float
    seconds_since_last_close: float | None
    source_candle_close_time_ms: int
    evaluated_at: dt.datetime
    action: str = _ACTION_OBSERVE_ONLY

    def to_evidence_dict(self) -> dict[str, Any]:
        """JSON-safe evidence row (Decimals → str). Always observe-only."""
        return {
            "action": self.action,
            "product": self.product,
            "symbol": self.symbol,
            "has_entry": self.has_entry,
            "side": self.side,
            "entry_price": None if self.entry_price is None else str(self.entry_price),
            "tp_price": None if self.tp_price is None else str(self.tp_price),
            "sl_price": None if self.sl_price is None else str(self.sl_price),
            "confidence": str(self.confidence),
            "signal_reason_codes": list(self.signal_reason_codes),
            "risk_allowed": self.risk_allowed,
            "risk_reason_codes": list(self.risk_reason_codes),
            "would_enter": self.would_enter,
            "spread_bps": str(self.spread_bps),
            "data_age_seconds": self.data_age_seconds,
            "seconds_since_last_close": self.seconds_since_last_close,
            "source_candle_close_time_ms": self.source_candle_close_time_ms,
            "evaluated_at": self.evaluated_at.isoformat(),
        }


async def evaluate_symbol(
    *,
    product: Product,
    symbol: str,
    market_data: MarketDataReader,
    ledger_snapshot: LedgerSnapshot,
    now: dt.datetime,
    limits: ScalpingRiskLimits | None = None,
    signal_config: SignalConfig | None = None,
    interval: str = "1m",
    limit: int = 50,
) -> ObserveOnlyRecord:
    """Produce one observe-only record for ``product``/``symbol``."""

    limits = limits or ScalpingRiskLimits()
    # Spot is long-only; futures may take the mirror short.
    config = signal_config or SignalConfig(allow_short=product == "usdm_futures")

    candles = await market_data.fetch_klines(
        product, symbol, interval=interval, limit=limit
    )
    book = await market_data.fetch_book_ticker(product, symbol)

    signal = evaluate_signal(candles, config)
    spread = spread_bps(book)
    latest = candles[-1]
    age = data_age_seconds(latest, now_ms=int(now.timestamp() * 1000))

    market = MarketConditions(
        spread_bps=spread,
        data_age_seconds=age,
        spot_free_base_qty=Decimal("0"),  # entry context; spot entries are BUY
    )

    if signal.has_entry and signal.side is not None:
        risk = evaluate_risk(
            product=product,
            symbol=symbol,
            side=signal.side,
            target_notional_usdt=limits.max_notional_usdt,
            limits=limits,
            ledger=ledger_snapshot,
            market=market,
        )
        risk_allowed = risk.allowed
        risk_reason_codes = risk.reason_codes
    else:
        risk_allowed = False
        risk_reason_codes = ()

    would_enter = signal.has_entry and risk_allowed

    return ObserveOnlyRecord(
        product=product,
        symbol=symbol,
        has_entry=signal.has_entry,
        side=signal.side,
        entry_price=signal.entry_price,
        tp_price=signal.tp_price,
        sl_price=signal.sl_price,
        confidence=signal.confidence,
        signal_reason_codes=signal.reason_codes,
        risk_allowed=risk_allowed,
        risk_reason_codes=risk_reason_codes,
        would_enter=would_enter,
        spread_bps=spread,
        data_age_seconds=age,
        seconds_since_last_close=ledger_snapshot.seconds_since_last_close_for_symbol,
        source_candle_close_time_ms=latest.close_time_ms,
        evaluated_at=now,
    )
