from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.symbol import to_db_symbol
from app.schemas.invest_feed_news import NewsMarket
from app.schemas.invest_stock_detail import (
    CandleCapability,
    StockDetailCandle,
    StockDetailCandlesResponse,
)
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    _normalize_crypto_market,
)

CandleProvider = Callable[[NewsMarket, str, str], Awaitable[list[dict[str, Any]]]]
_INTRADAY_PERIODS = {"1m", "5m", "10m", "15m", "30m", "60m", "1h"}
_CRYPTO_PERIODS = {"1d", "day", "1w", "week", "1mo", "month"}


class UnsupportedPeriod(ValueError):
    def __init__(self, market: NewsMarket, period: str) -> None:
        super().__init__(f"unsupported_period: {market}/{period}")
        self.market = market
        self.period = period
        self.code = "unsupported_period"


async def _empty_provider(
    market: NewsMarket, symbol: str, period: str
) -> list[dict[str, Any]]:
    return []


def _canonical_symbol(market: NewsMarket, symbol: str) -> str:
    if market == "us":
        return to_db_symbol(symbol.strip().upper())
    if market == "crypto":
        return _normalize_crypto_market(symbol)
    return symbol.strip().upper()


def _source_for(market: NewsMarket, period: str) -> str:
    if market == "kr":
        return "kis"
    if market == "us":
        return "yahoo"
    return "upbit"


async def build_stock_detail_candles(
    *,
    market: NewsMarket,
    symbol: str,
    period: str = "1d",
    provider: CandleProvider = _empty_provider,
) -> StockDetailCandlesResponse:
    if market == "crypto" and period in _INTRADAY_PERIODS:
        raise UnsupportedPeriod(market, period)
    if market == "crypto" and period not in _CRYPTO_PERIODS:
        raise UnsupportedPeriod(market, period)

    canonical = _canonical_symbol(market, symbol)
    rows = await provider(market, canonical, period)
    candles = [
        StockDetailCandle(
            ts=row.get("ts")
            or row.get("timestamp")
            or row.get("date")
            or datetime.now(UTC),
            open=float(row.get("open") or row.get("o") or 0),
            high=float(row.get("high") or row.get("h") or 0),
            low=float(row.get("low") or row.get("l") or 0),
            close=float(row.get("close") or row.get("c") or 0),
            volume=float(row["volume"]) if row.get("volume") is not None else None,
        )
        for row in rows
    ]
    return StockDetailCandlesResponse(
        symbol=canonical,
        market=market,
        period=period,
        source=_source_for(market, period),
        candles=candles,
        capabilities=CandleCapability(
            supported=True, intradaySupported=market != "crypto"
        ),
    )


__all__ = ["UnsupportedPeriod", "build_stock_detail_candles"]
