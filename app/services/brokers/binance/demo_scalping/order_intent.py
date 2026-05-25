"""ROB-307 PR2 — order-intent contract (signal → executor bridge).

``OrderIntent`` is the explicit, validated hand-off from the read-only
signal layer to the one-shot Demo executor. It is pure data — no broker,
no DB, no execution imports — so it lives in the read-only package and is
covered by the import guard. The executor re-fetches live price/filters
for sizing; the intent carries the risk-approved notional cap, the
candidate TP/SL (for ledger/audit metadata), reason codes, and source
timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.brokers.binance.demo_scalping.contract import (
    Product,
    ScalpingRiskLimits,
    Side,
)
from app.services.brokers.binance.demo_scalping.signal import SignalDecision


@dataclass(frozen=True)
class OrderIntent:
    product: Product
    symbol: str
    side: Side
    order_type: str
    target_notional_usdt: Decimal
    entry_reference_price: Decimal | None
    tp_price: Decimal | None
    sl_price: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...]
    source_candle_close_time_ms: int
    evaluated_at_ms: int

    def to_evidence_dict(self) -> dict[str, Any]:
        """JSON-safe metadata for ledger ``extra_metadata`` / evidence."""
        return {
            "product": self.product,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "target_notional_usdt": str(self.target_notional_usdt),
            "entry_reference_price": (
                None
                if self.entry_reference_price is None
                else str(self.entry_reference_price)
            ),
            "tp_price": None if self.tp_price is None else str(self.tp_price),
            "sl_price": None if self.sl_price is None else str(self.sl_price),
            "confidence": str(self.confidence),
            "reason_codes": list(self.reason_codes),
            "source_candle_close_time_ms": self.source_candle_close_time_ms,
            "evaluated_at_ms": self.evaluated_at_ms,
        }


def build_order_intent(
    signal: SignalDecision,
    *,
    product: Product,
    symbol: str,
    limits: ScalpingRiskLimits,
    source_candle_close_time_ms: int,
    evaluated_at_ms: int,
    order_type: str = "MARKET",
) -> OrderIntent | None:
    """Build an ``OrderIntent`` from an entry signal, else ``None``.

    The notional is pinned to the risk cap (``limits.max_notional_usdt``);
    the executor floors it to exchange filters and never rounds up.
    """
    if not signal.has_entry or signal.side is None:
        return None
    return OrderIntent(
        product=product,
        symbol=symbol,
        side=signal.side,
        order_type=order_type,
        target_notional_usdt=limits.max_notional_usdt,
        entry_reference_price=signal.entry_price,
        tp_price=signal.tp_price,
        sl_price=signal.sl_price,
        confidence=signal.confidence,
        reason_codes=signal.reason_codes,
        source_candle_close_time_ms=source_candle_close_time_ms,
        evaluated_at_ms=evaluated_at_ms,
    )
