"""Exact decimal sizing and identities for manual crypto paper episodes."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from math import lcm


def positive(value: object) -> Decimal:
    if isinstance(value, (bool, float)):
        raise ValueError("use exact decimal text")
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("expected finite positive decimal")
    return number


class BelowVenueMinimum(ValueError):
    """A step-floored residual cannot be submitted without rounding up."""


@dataclass(frozen=True)
class MarketRules:
    step: Decimal
    min_qty: Decimal
    min_notional: Decimal
    max_qty: Decimal | None = None
    max_notional: Decimal | None = None

    def floor(self, qty: Decimal, price: Decimal) -> Decimal:
        qty, price, step = positive(qty), positive(price), positive(self.step)
        qty = (qty / step).to_integral_value(rounding=ROUND_DOWN) * step
        if qty <= 0 or qty < self.min_qty or qty * price < self.min_notional:
            raise BelowVenueMinimum("below venue minimum after step floor")
        if self.max_qty is not None and qty > self.max_qty:
            raise ValueError("above venue quantity maximum")
        if self.max_notional is not None and qty * price > self.max_notional:
            raise ValueError("above venue notional maximum")
        return qty


def binance_rules(info: dict) -> MarketRules:
    if info.get("status") != "TRADING" or info.get("quoteAsset") != "USDT":
        raise ValueError("not a trading USDT contract")
    if "contractType" in info and info["contractType"] != "PERPETUAL":
        raise ValueError("only perpetual futures supported")
    filters = {f["filterType"]: f for f in info["filters"]}
    lot = filters["LOT_SIZE"]
    lots = [lot]
    market_lot = filters.get("MARKET_LOT_SIZE")
    if market_lot is not None:
        lots.append(market_lot)
    steps = [positive(f["stepSize"]) for f in lots if Decimal(f["stepSize"]) > 0]
    scale = 10 ** max(max(0, -s.as_tuple().exponent) for s in steps)
    step = Decimal(lcm(*(int(s * scale) for s in steps))) / scale
    minimum = filters.get("NOTIONAL", filters.get("MIN_NOTIONAL"))
    if minimum is None:
        raise ValueError("venue minimum notional missing")
    min_notional = positive(minimum.get("minNotional", minimum.get("notional")))
    maxima = [positive(f["maxQty"]) for f in lots if Decimal(f["maxQty"]) > 0]
    return MarketRules(
        step=step,
        min_qty=max(Decimal(f["minQty"]) for f in lots),
        min_notional=min_notional,
        max_qty=min(maxima) if maxima else None,
        max_notional=positive(minimum["maxNotional"])
        if minimum.get("applyMaxToMarket") is True
        else None,
    )


def strategy_id(horizon: int) -> str:
    return f"rob1474.crypto.relative_volatility_20.h{horizon}.v1"


def client_id(*parts: str, closing: bool = False) -> str:
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()[:22]
    return f"r1474c-{'x' if closing else 'e'}-{digest}"
