from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from app.services.exchange_rate_service import (
    UsdKrwExchangeRateQuote,
    get_usd_krw_rate_details,
)

logger = logging.getLogger(__name__)

FX_RATE_SOURCE_RECONCILE_SPOT = "reconcile_spot"
FX_RATE_SOURCE_MANUAL = "manual"
FX_RATE_SOURCE_UNAVAILABLE = "unavailable"
FX_PNL_ACCURACY_APPROXIMATE = "approximate"
FX_PNL_ACCURACY_EXACT = "exact"
FX_PNL_ACCURACY_UNAVAILABLE = "unavailable"

_MONEY_4 = Decimal("0.0001")


@dataclass(frozen=True)
class FxRateCapture:
    rate: Decimal | None
    fx_rate_source: str
    fx_pnl_accuracy: str


def _q4(value: Decimal) -> Decimal:
    return value.quantize(_MONEY_4, rounding=ROUND_HALF_UP)


def compute_us_equity_fx_pnl(
    *,
    buy_price: Decimal,
    sell_price: Decimal,
    quantity: Decimal,
    buy_fx_rate: Decimal | None,
    sell_fx_rate: Decimal | None,
) -> dict[str, Decimal] | None:
    if buy_fx_rate is None or sell_fx_rate is None:
        return None
    if quantity <= 0:
        return None

    buy_notional_usd = buy_price * quantity
    sell_notional_usd = sell_price * quantity
    security_pnl_usd = sell_notional_usd - buy_notional_usd
    security_pnl_krw = security_pnl_usd * sell_fx_rate
    fx_pnl_krw = buy_notional_usd * (sell_fx_rate - buy_fx_rate)
    total_pnl_krw = security_pnl_krw + fx_pnl_krw
    identity_total = (sell_notional_usd * sell_fx_rate) - (
        buy_notional_usd * buy_fx_rate
    )

    return {
        "buy_notional_usd": _q4(buy_notional_usd),
        "sell_notional_usd": _q4(sell_notional_usd),
        "security_pnl_usd": _q4(security_pnl_usd),
        "security_pnl_krw": _q4(security_pnl_krw),
        "fx_pnl_krw": _q4(fx_pnl_krw),
        "total_pnl_krw": _q4(total_pnl_krw),
        "identity_total_pnl_krw": _q4(identity_total),
    }


async def capture_reconcile_spot_fx() -> FxRateCapture:
    try:
        quote = await get_usd_krw_rate_details()
    except Exception as exc:
        logger.warning("USD/KRW reconcile-spot capture failed: %s", exc)
        return FxRateCapture(
            rate=None,
            fx_rate_source=FX_RATE_SOURCE_UNAVAILABLE,
            fx_pnl_accuracy=FX_PNL_ACCURACY_UNAVAILABLE,
        )
    return FxRateCapture(
        rate=Decimal(str(quote.default_rate)),
        fx_rate_source=FX_RATE_SOURCE_RECONCILE_SPOT,
        fx_pnl_accuracy=FX_PNL_ACCURACY_APPROXIMATE,
    )


__all__ = [
    "FX_PNL_ACCURACY_APPROXIMATE",
    "FX_PNL_ACCURACY_EXACT",
    "FX_PNL_ACCURACY_UNAVAILABLE",
    "FX_RATE_SOURCE_MANUAL",
    "FX_RATE_SOURCE_RECONCILE_SPOT",
    "FX_RATE_SOURCE_UNAVAILABLE",
    "FxRateCapture",
    "UsdKrwExchangeRateQuote",
    "capture_reconcile_spot_fx",
    "compute_us_equity_fx_pnl",
]
