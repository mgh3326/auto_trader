"""Phase 1 — pure daily buy&hold benchmark math for the demo scalping review.

No DB, no network, no market-data client. The market-data fetch + storage
live in ``demo_scalping_exec.benchmark_runner``; this module stays pure so it
is trivially testable and safe to import from the review service boundary.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

_BPS = Decimal("10000")


def daily_buy_and_hold_return_bps(
    *, open_price: Decimal, close_price: Decimal
) -> Decimal:
    """Passive buy&hold return over the day in bps: ``(close/open - 1) * 1e4``."""
    if open_price <= 0:
        raise ValueError(f"open_price must be > 0, got {open_price}")
    return (close_price / open_price - Decimal("1")) * _BPS


def notional_weighted_benchmark_bps(
    weighted: Sequence[tuple[Decimal, Decimal]],
) -> Decimal | None:
    """Notional-weighted mean of per-symbol benchmark bps.

    ``weighted`` is a sequence of ``(notional_usdt, benchmark_bps)`` pairs.
    Mirrors the strategy ``net_return_bps`` capital-weighting (rollup.py) so
    strategy vs benchmark are comparable. Returns ``None`` when there is no
    positive notional to weight by."""
    total_notional = sum((n for n, _ in weighted), Decimal("0"))
    if total_notional <= 0:
        return None
    weighted_sum = sum((n * b for n, b in weighted), Decimal("0"))
    return weighted_sum / total_notional
