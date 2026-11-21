from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from app.services.upbit_krw_strategy.types import DailyBar, MarketInput

ASOF = date(2026, 11, 22)  # issue slot date (UTC); newest closed candle = 11-21
LAST = date(2026, 11, 21)


def bars(
    *,
    count: int,
    close: str = "1000",
    quote_volume: str | None = "10000000000",
    last: date = LAST,
    is_closed: bool = True,
) -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            day=last - timedelta(days=k),
            close=Decimal(close),
            quote_volume=None if quote_volume is None else Decimal(quote_volume),
            is_closed=is_closed,
        )
        for k in range(count)
    )


def market(
    name: str = "KRW-ETH",
    *,
    is_active: bool = True,
    warning: str = "NONE",
    bar_set: tuple[DailyBar, ...] | None = None,
) -> MarketInput:
    return MarketInput(
        market=name,
        is_active=is_active,
        market_warning=warning,
        bars=bars(count=90) if bar_set is None else bar_set,
    )
