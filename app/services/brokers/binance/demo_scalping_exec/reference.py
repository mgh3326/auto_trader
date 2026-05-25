"""ROB-307 PR2 — read-only demo-host reference data (price + sizing filters).

Fetches the MARKET sizing step, MIN_NOTIONAL, and a reference price from
exchangeInfo + ticker/price on Demo hosts only (reusing the read-only
``build_demo_data_client`` host-allowlist transport). Unsigned, no
credentials. Used by the executor for sizing before any signed order.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx

from app.services.brokers.binance.demo_scalping.contract import Product
from app.services.brokers.binance.demo_scalping.market_data import (
    build_demo_data_client,
)

_BASE_URL: dict[str, str] = {
    "spot": "https://demo-api.binance.com",
    "usdm_futures": "https://demo-fapi.binance.com",
}
_EXCHANGE_INFO_PATH: dict[str, str] = {
    "spot": "/api/v3/exchangeInfo",
    "usdm_futures": "/fapi/v1/exchangeInfo",
}
_PRICE_PATH: dict[str, str] = {
    "spot": "/api/v3/ticker/price",
    "usdm_futures": "/fapi/v1/ticker/price",
}


class ReferenceDataError(RuntimeError):
    """Raised when exchangeInfo/price lacks usable data for a symbol."""


@dataclass(frozen=True)
class SymbolReference:
    price: Decimal
    step_size: Decimal  # MARKET sizing step (LOT_SIZE / MARKET_LOT_SIZE)
    min_notional: Decimal
    tick_size: Decimal  # PRICE_FILTER tick — for tick-aligning bracket prices


def _find_symbol_row(body: dict[str, Any], symbol: str) -> dict[str, Any]:
    for row in body.get("symbols", []):
        if row.get("symbol") == symbol:
            return row
    raise ReferenceDataError(f"exchangeInfo has no row for {symbol!r}")


def _parse_filters(row: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    """Return ``(market_step, min_notional, tick_size)`` from a symbol row."""
    filters = {f.get("filterType"): f for f in row.get("filters", [])}

    # MARKET orders use MARKET_LOT_SIZE when present and non-zero, else LOT_SIZE.
    step: Decimal | None = None
    market_lot = filters.get("MARKET_LOT_SIZE")
    if market_lot and Decimal(str(market_lot.get("stepSize", "0"))) > 0:
        step = Decimal(str(market_lot["stepSize"]))
    if step is None:
        lot = filters.get("LOT_SIZE")
        if lot and Decimal(str(lot.get("stepSize", "0"))) > 0:
            step = Decimal(str(lot["stepSize"]))
    if step is None:
        raise ReferenceDataError("no usable LOT_SIZE/MARKET_LOT_SIZE step")

    # MIN_NOTIONAL (futures: 'notional'; spot: 'minNotional') or NOTIONAL.
    min_notional = Decimal("0")
    for filter_type in ("MIN_NOTIONAL", "NOTIONAL"):
        flt = filters.get(filter_type)
        if not flt:
            continue
        for key in ("minNotional", "notional"):
            if flt.get(key) is not None:
                min_notional = Decimal(str(flt[key]))
                break
        if min_notional > 0:
            break

    # PRICE_FILTER tickSize — needed to tick-align bracket stop/limit prices.
    price_filter = filters.get("PRICE_FILTER")
    if not price_filter or Decimal(str(price_filter.get("tickSize", "0"))) <= 0:
        raise ReferenceDataError("no usable PRICE_FILTER tickSize")
    tick_size = Decimal(str(price_filter["tickSize"]))

    return step, min_notional, tick_size


class DemoReferenceData:
    """Read-only demo-host reference reader (exchangeInfo + ticker/price)."""

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        self._client = client or build_demo_data_client()
        self._owns_client = client is None

    async def fetch(self, product: Product, symbol: str) -> SymbolReference:
        base = _BASE_URL[product]
        info = await self._client.get(
            base + _EXCHANGE_INFO_PATH[product], params={"symbol": symbol}
        )
        info.raise_for_status()
        step, min_notional, tick_size = _parse_filters(
            _find_symbol_row(info.json(), symbol)
        )

        price_resp = await self._client.get(
            base + _PRICE_PATH[product], params={"symbol": symbol}
        )
        price_resp.raise_for_status()
        raw_price = price_resp.json().get("price")
        if raw_price is None:
            raise ReferenceDataError(f"ticker/price returned no price for {symbol!r}")

        return SymbolReference(
            price=Decimal(str(raw_price)),
            step_size=step,
            min_notional=min_notional,
            tick_size=tick_size,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
