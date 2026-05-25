"""ROB-313 PR1 — deterministic scalping cost computation (pure).

No network, no DB, no broker. Slippage is measured **exactly** from the
actual fill vs the intended reference price; fees are **estimated** from a
config fee-rate (Demo "exact" commission is not real-VIP/BNB-accurate, so a
consistent model shared with the backtester is more useful — ROB-313 D3).

Sign convention for ``slippage_bps``: **positive = adverse** (worse than the
reference). A BUY pays up (fill above reference is adverse); a SELL receives
less (fill below reference is adverse).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.services.brokers.binance.demo_scalping.contract import Side

_BPS = Decimal("10000")


def slippage_bps(
    *, fill_price: Decimal, reference_price: Decimal, side: Side
) -> Decimal:
    """Adverse-positive execution slippage in bps, relative to ``reference_price``."""
    if reference_price <= 0:
        raise ValueError(f"reference_price must be > 0, got {reference_price}")
    if side == "BUY":
        return (fill_price - reference_price) / reference_price * _BPS
    if side == "SELL":
        return (reference_price - fill_price) / reference_price * _BPS
    raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")


def fee_estimate_usdt(*, notional_usdt: Decimal, fee_rate_bps: Decimal) -> Decimal:
    """Estimated one-leg fee in USDT: ``notional * fee_rate_bps / 10_000``."""
    if fee_rate_bps < 0:
        raise ValueError(f"fee_rate_bps must be >= 0, got {fee_rate_bps}")
    return notional_usdt * fee_rate_bps / _BPS


def net_pnl_usdt(
    *,
    side: Side,
    entry_price: Decimal,
    exit_price: Decimal,
    qty: Decimal,
    entry_fee_usdt: Decimal,
    exit_fee_usdt: Decimal,
) -> Decimal:
    """Round-trip net PnL in USDT. ``side`` is the **entry** side. funding=0 (MVP).

    LONG (entry BUY):  (exit - entry) * qty - fees
    SHORT (entry SELL): (entry - exit) * qty - fees
    """
    if side == "BUY":
        gross = (exit_price - entry_price) * qty
    elif side == "SELL":
        gross = (entry_price - exit_price) * qty
    else:
        raise ValueError(f"side must be 'BUY' or 'SELL', got {side!r}")
    return gross - entry_fee_usdt - exit_fee_usdt


def net_return_bps(*, net_pnl_usdt: Decimal, entry_notional_usdt: Decimal) -> Decimal:
    """Per-trade return in bps relative to the entry notional."""
    if entry_notional_usdt <= 0:
        raise ValueError(f"entry_notional_usdt must be > 0, got {entry_notional_usdt}")
    return net_pnl_usdt / entry_notional_usdt * _BPS


def spot_avg_fill_price(
    *, cummulative_quote_qty: Decimal, executed_qty: Decimal
) -> Decimal | None:
    """Spot has no ``avgPrice`` field — derive it from the fill aggregates.

    Returns ``None`` when nothing executed (the caller treats an unfilled
    leg as an anomaly rather than fabricating a price). Futures responses
    carry ``avgPrice`` directly and do not need this.
    """
    if executed_qty <= 0:
        return None
    return cummulative_quote_qty / executed_qty


@dataclass(frozen=True)
class RoundTripEconomics:
    """Computed economics for one open+close round-trip. Exit-derived fields
    are ``None`` when the close leg did not fill (anomaly) — never fabricated."""

    entry_notional_usdt: Decimal
    entry_fee_usdt: Decimal
    entry_slippage_bps: Decimal
    exit_fee_usdt: Decimal | None
    exit_slippage_bps: Decimal | None
    gross_pnl_usdt: Decimal | None
    net_pnl_usdt: Decimal | None
    net_return_bps: Decimal | None


def build_round_trip_economics(
    *,
    side: Side,
    qty: Decimal,
    entry_reference_price: Decimal,
    entry_fill_price: Decimal,
    fee_rate_bps: Decimal,
    exit_fill_price: Decimal | None = None,
    exit_reference_price: Decimal | None = None,
) -> RoundTripEconomics:
    """Assemble round-trip economics from raw fills (pure; uses the helpers
    above). ``side`` is the **entry** side; the exit leg is the opposite."""
    entry_notional = entry_fill_price * qty
    entry_fee = fee_estimate_usdt(
        notional_usdt=entry_notional, fee_rate_bps=fee_rate_bps
    )
    entry_slip = slippage_bps(
        fill_price=entry_fill_price, reference_price=entry_reference_price, side=side
    )

    if exit_fill_price is None:
        return RoundTripEconomics(
            entry_notional_usdt=entry_notional,
            entry_fee_usdt=entry_fee,
            entry_slippage_bps=entry_slip,
            exit_fee_usdt=None,
            exit_slippage_bps=None,
            gross_pnl_usdt=None,
            net_pnl_usdt=None,
            net_return_bps=None,
        )

    exit_notional = exit_fill_price * qty
    exit_fee = fee_estimate_usdt(notional_usdt=exit_notional, fee_rate_bps=fee_rate_bps)
    exit_side: Side = "SELL" if side == "BUY" else "BUY"
    exit_slip = (
        slippage_bps(
            fill_price=exit_fill_price,
            reference_price=exit_reference_price,
            side=exit_side,
        )
        if exit_reference_price is not None
        else None
    )
    gross = net_pnl_usdt(
        side=side,
        entry_price=entry_fill_price,
        exit_price=exit_fill_price,
        qty=qty,
        entry_fee_usdt=Decimal("0"),
        exit_fee_usdt=Decimal("0"),
    )
    net = gross - entry_fee - exit_fee
    return RoundTripEconomics(
        entry_notional_usdt=entry_notional,
        entry_fee_usdt=entry_fee,
        entry_slippage_bps=entry_slip,
        exit_fee_usdt=exit_fee,
        exit_slippage_bps=exit_slip,
        gross_pnl_usdt=gross,
        net_pnl_usdt=net,
        net_return_bps=net_return_bps(
            net_pnl_usdt=net, entry_notional_usdt=entry_notional
        ),
    )
