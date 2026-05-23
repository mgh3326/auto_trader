"""ROB-298 PR 2 — Futures Demo order sizing.

Reuses the floor-only LOT_SIZE / MIN_NOTIONAL semantics from Spot Demo
(``app/services/brokers/binance/spot_demo/sizing.py``) plus an explicit
symbol allowlist for futures.

BTCUSDT is excluded: MIN_NOTIONAL=50 USDT > 10 USDT cap → always blocked.
Default symbol is XRPUSDT (MIN_NOTIONAL=5, LOT_SIZE step=0.1).

Operator can extend the allowlist via override at call time, but the
excluded list always wins — you cannot un-exclude BTCUSDT via override.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoUnsupportedSymbol,
)

FUTURES_DEMO_DEFAULT_SYMBOL = "XRPUSDT"

FUTURES_DEMO_FALLBACK_SYMBOLS: frozenset[str] = frozenset(
    {"XRPUSDT", "DOGEUSDT", "SOLUSDT"}
)

FUTURES_DEMO_EXCLUDED_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT"})


@dataclass(frozen=True)
class FuturesSizingResult:
    qty: Decimal
    notional_usdt: Decimal


@dataclass(frozen=True)
class FuturesSizingBlocked:
    reason: str


def assert_symbol_allowed(
    symbol: str,
    *,
    allowlist_override: frozenset[str] | None = None,
) -> None:
    """Raise ``BinanceFuturesDemoUnsupportedSymbol`` for excluded/non-allowlisted symbols.

    The excluded list (e.g. BTCUSDT) is checked first and cannot be
    bypassed by ``allowlist_override``.
    """
    if symbol in FUTURES_DEMO_EXCLUDED_SYMBOLS:
        raise BinanceFuturesDemoUnsupportedSymbol(
            f"{symbol} is explicitly excluded (MIN_NOTIONAL > 10 USDT cap)"
        )
    allowlist = (
        allowlist_override
        if allowlist_override is not None
        else FUTURES_DEMO_FALLBACK_SYMBOLS
    )
    if symbol not in allowlist:
        raise BinanceFuturesDemoUnsupportedSymbol(
            f"{symbol} not in allowlist {sorted(allowlist)}"
        )


def compute_futures_demo_order_qty(
    *,
    symbol: str,
    target_notional_usdt: Decimal,
    price: Decimal,
    min_notional: Decimal,
    step_size: Decimal,
    cap_usdt: Decimal,
    symbol_allowlist_override: frozenset[str] | None = None,
) -> FuturesSizingResult | FuturesSizingBlocked:
    assert_symbol_allowed(symbol, allowlist_override=symbol_allowlist_override)
    if cap_usdt <= 0:
        raise ValueError("cap_usdt must be > 0")
    if price <= 0:
        raise ValueError("price must be > 0")
    if step_size <= 0:
        raise ValueError("step_size must be > 0")

    effective_target = min(target_notional_usdt, cap_usdt)
    raw_qty = effective_target / price
    floored_qty = (raw_qty / step_size).quantize(
        Decimal("1"), rounding=ROUND_DOWN
    ) * step_size
    if floored_qty <= 0:
        return FuturesSizingBlocked(
            reason=(
                f"floored qty=0 < MIN_NOTIONAL={min_notional} "
                f"(target={effective_target} / price={price} < step_size={step_size})"
            )
        )
    notional = floored_qty * price
    if notional < min_notional:
        return FuturesSizingBlocked(
            reason=(
                f"notional={notional} < MIN_NOTIONAL={min_notional} "
                f"after LOT_SIZE floor (qty={floored_qty})"
            )
        )
    if notional > cap_usdt:
        return FuturesSizingBlocked(
            reason=f"computed notional={notional} > cap={cap_usdt} (sizing bug)"
        )
    return FuturesSizingResult(qty=floored_qty, notional_usdt=notional)
