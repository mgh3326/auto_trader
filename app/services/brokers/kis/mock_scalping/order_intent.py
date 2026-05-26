"""ROB-321 PR3 — order-intent contract (signal → executor bridge).

``OrderIntent`` is the explicit, validated hand-off from the read-only signal
layer to the PR4 mock executor. It is pure data — no broker, no DB, no execution
imports — so it lives in the read-only package. The executor re-fetches live
price/tick-size for sizing; the intent carries the risk-approved KRW notional
cap, the candidate TP/SL (for ledger/audit metadata), reason codes, and source
timestamps.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.brokers.kis.mock_scalping.contract import ScalpingRiskLimits, Side
from app.services.brokers.kis.mock_scalping.signal import SignalDecision


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: Side
    order_type: str
    target_notional_krw: Decimal
    entry_reference_price: Decimal | None
    tp_price: Decimal | None
    sl_price: Decimal | None
    confidence: Decimal
    reason_codes: tuple[str, ...]
    source_candle_close_time_ms: int
    evaluated_at_ms: int
    account_mode: str = "kis_mock"

    def to_evidence_dict(self) -> dict[str, Any]:
        """JSON-safe metadata for ledger / evidence."""
        return {
            "account_mode": self.account_mode,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "target_notional_krw": str(self.target_notional_krw),
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
    symbol: str,
    limits: ScalpingRiskLimits,
    source_candle_close_time_ms: int,
    evaluated_at_ms: int,
    order_type: str = "limit",
) -> OrderIntent | None:
    """Build an ``OrderIntent`` from a long entry signal, else ``None``.

    The notional is pinned to the risk cap (``limits.max_notional_krw``); the
    executor floors it to the share price / tick size and never rounds up.
    Returns None for non-entry or non-BUY signals (cash market is long-only).
    """
    if not signal.has_entry or signal.side != "BUY":
        return None
    return OrderIntent(
        symbol=symbol,
        side=signal.side,
        order_type=order_type,
        target_notional_krw=limits.max_notional_krw,
        entry_reference_price=signal.entry_price,
        tp_price=signal.tp_price,
        sl_price=signal.sl_price,
        confidence=signal.confidence,
        reason_codes=signal.reason_codes,
        source_candle_close_time_ms=source_candle_close_time_ms,
        evaluated_at_ms=evaluated_at_ms,
    )
