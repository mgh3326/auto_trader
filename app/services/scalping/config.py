"""ROB-286 — Scalper configuration (MVP defaults).

The MVP symbol set is hard-coded (deliberate friction; expansion is a
code change reviewable separately). Indicator thresholds + risk params
are conservative — small notional, tight stops, RSI 30/70 boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class ScalperConfig:
    """Per-runner configuration.

    All fields are immutable; spawn a new instance to change anything.
    """

    # MVP symbols — locked set per §B.C.8.
    symbols: frozenset[str] = field(
        default_factory=lambda: frozenset({"BTCUSDT", "ETHUSDT", "SOLUSDT"})
    )

    # Sizing
    max_notional_usdt: Decimal = Decimal("10")

    # Indicator thresholds (5-minute timeframe)
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0

    # Take-profit + stop-loss percentages (relative to entry price).
    # Conservative defaults; the smoke CLI keeps these constant.
    tp_pct: Decimal = Decimal("0.005")  # +0.5%
    sl_pct: Decimal = Decimal("0.003")  # -0.3%

    # Reconciliation depth (open item #3 lean — last 50 orders / 100 fills
    # / max 24h time bound).
    reconcile_open_orders_limit: int = 50
    reconcile_recent_fills_limit: int = 100
    reconcile_lookback_hours: int = 24

    @classmethod
    def default_for_testnet(cls) -> ScalperConfig:
        return cls()
