"""ROB-993 — sizing + public exchangeInfo/price reads for the strategy loop.

Reuses ``futures_demo.sizing`` (LOT_SIZE floor, MIN_NOTIONAL guard, symbol
allowlist) for the notional -> qty conversion. Adds only the two Binance
public-GET helpers (exchangeInfo filters + reference price, unsigned,
demo-fapi host) and the final quantity-string quantization the ROB-298
PR 2 smoke CLI already proved necessary (avoids -1111 "precision over the
maximum" — see ``scripts/binance_futures_demo_smoke.py::_quantize_qty``).
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Final

import httpx

from app.services.brokers.binance.futures_demo.sizing import (
    FUTURES_DEMO_EXCLUDED_SYMBOLS,
    FUTURES_DEMO_FALLBACK_SYMBOLS,
    FuturesSizingBlocked,
    FuturesSizingResult,
    assert_symbol_allowed,
    compute_futures_demo_order_qty,
)

__all__ = [
    "FUTURES_DEMO_EXCLUDED_SYMBOLS",
    "FUTURES_DEMO_FALLBACK_SYMBOLS",
    "FuturesSizingBlocked",
    "FuturesSizingResult",
    "assert_symbol_allowed",
    "compute_futures_demo_order_qty",
    "quantize_qty",
    "fetch_symbol_filters",
    "fetch_reference_price",
    "LEG_NOTIONAL_CAP_MIN_USDT",
    "LEG_NOTIONAL_CAP_MAX_USDT",
    "LegNotionalCapNotLocked",
    "assert_leg_notional_cap_locked",
]

_EXCHANGE_INFO_PATH = "/fapi/v1/exchangeInfo"
_PRICE_PATH = "/fapi/v1/ticker/price"

# ROB-993 adversarial review (verify-993-2256.md, Finding 1) — the ROB-993
# ticket/CLAUDE.md locks this lane's leg size at "$6~10"; that is a hard
# safety invariant, not an operator-tunable dial. There is deliberately no
# CLI flag to set ``cap_usdt`` — ``assert_leg_notional_cap_locked`` fails
# closed BEFORE any network/DB call if a caller (a future strategy adapter,
# a test, a scheduler) supplies a value outside the locked range.
LEG_NOTIONAL_CAP_MIN_USDT: Final[Decimal] = Decimal("6")
LEG_NOTIONAL_CAP_MAX_USDT: Final[Decimal] = Decimal("10")


class LegNotionalCapNotLocked(ValueError):
    """Raised when a supplied ``cap_usdt`` falls outside the locked lane range."""


def assert_leg_notional_cap_locked(cap_usdt: Decimal) -> None:
    if not (LEG_NOTIONAL_CAP_MIN_USDT <= cap_usdt <= LEG_NOTIONAL_CAP_MAX_USDT):
        raise LegNotionalCapNotLocked(
            f"cap_usdt={cap_usdt} outside the locked lane invariant "
            f"[{LEG_NOTIONAL_CAP_MIN_USDT}, {LEG_NOTIONAL_CAP_MAX_USDT}] — this "
            "lane has no operator-tunable leg size"
        )


def quantize_qty(
    qty: Decimal, *, step_size: Decimal, quantity_precision: int | None
) -> Decimal:
    """Round ``qty`` DOWN to a Binance-submittable precision (ROB-302 Codex #6)."""
    if quantity_precision is not None:
        target = Decimal(1).scaleb(-quantity_precision)
    else:
        exponent = step_size.normalize().as_tuple().exponent
        target = (
            Decimal(1).scaleb(exponent)
            if isinstance(exponent, int) and exponent < 0
            else Decimal(1)
        )
    return qty.quantize(target, rounding=ROUND_DOWN)


def _parse_symbol_filters(body: dict[str, Any], symbol: str) -> dict[str, Any]:
    row: dict[str, Any] | None = None
    for entry in body.get("symbols") or []:
        if entry.get("symbol") == symbol:
            row = entry
            break
    if row is None:
        raise RuntimeError(f"exchangeInfo has no row for {symbol!r}")

    market_step: Decimal | None = None
    lot_step: Decimal | None = None
    min_notional: Decimal | None = None
    for entry in row.get("filters") or []:
        ftype = entry.get("filterType")
        if ftype == "MARKET_LOT_SIZE":
            market_step = Decimal(str(entry.get("stepSize", "0")))
        elif ftype == "LOT_SIZE":
            lot_step = Decimal(str(entry.get("stepSize", "0")))
        elif ftype in ("MIN_NOTIONAL", "NOTIONAL"):
            mn = (
                entry.get("notional")
                or entry.get("minNotional")
                or entry.get("minNotionalValue")
            )
            if mn is not None:
                min_notional = Decimal(str(mn))

    market_usable = market_step is not None and market_step > 0
    step_size = market_step if market_usable else lot_step
    if step_size is None or step_size <= 0:
        raise RuntimeError(
            f"no usable LOT_SIZE/MARKET_LOT_SIZE step in exchangeInfo for {symbol!r}"
        )
    if min_notional is None:
        min_notional = Decimal("5")

    qp = row.get("quantityPrecision")
    return {
        "step_size": step_size,
        "min_notional": min_notional,
        "quantity_precision": int(qp) if qp is not None else None,
    }


async def fetch_symbol_filters(
    client: httpx.AsyncClient, symbol: str
) -> dict[str, Any]:
    """Public unsigned read — MARKET_LOT_SIZE/LOT_SIZE step + MIN_NOTIONAL."""
    resp = await client.get(_EXCHANGE_INFO_PATH, params={"symbol": symbol})
    resp.raise_for_status()
    return _parse_symbol_filters(resp.json(), symbol)


async def fetch_reference_price(client: httpx.AsyncClient, symbol: str) -> Decimal:
    """Public unsigned read — latest mark price for ``symbol``."""
    resp = await client.get(_PRICE_PATH, params={"symbol": symbol})
    resp.raise_for_status()
    price = resp.json().get("price")
    if price is None:
        raise RuntimeError(f"ticker/price returned no price for {symbol!r}")
    return Decimal(str(price))
