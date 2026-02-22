from __future__ import annotations

import datetime as dt
from typing import Any

import pandas as pd

from app.core.async_rate_limiter import RateLimitExceededError
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit.client import fetch_multiple_current_prices
from app.services.brokers.upbit.client import fetch_ohlcv as fetch_upbit_ohlcv
from app.services.brokers.yahoo.client import fetch_fast_info
from app.services.brokers.yahoo.client import fetch_ohlcv as fetch_yahoo_ohlcv
from app.services.domain_errors import (
    RateLimitError,
    SymbolNotFoundError,
    UpstreamUnavailableError,
    ValidationError,
)
from app.services.market_data.contracts import Candle, Quote


def _normalize_market(market: str) -> str:
    normalized = str(market or "").strip().lower()
    aliases = {
        "kr": "equity_kr",
        "kospi": "equity_kr",
        "kosdaq": "equity_kr",
        "us": "equity_us",
        "nasdaq": "equity_us",
        "nyse": "equity_us",
        "crypto": "crypto",
        "upbit": "crypto",
    }
    resolved = aliases.get(normalized, normalized)
    if resolved not in {"equity_kr", "equity_us", "crypto"}:
        raise ValidationError(f"Unsupported market: {market}")
    return resolved


def _normalize_symbol(symbol: str, market: str) -> str:
    value = str(symbol or "").strip()
    if not value:
        raise ValidationError("symbol is required")
    if market == "crypto":
        upper = value.upper()
        if upper.startswith(("KRW-", "USDT-")):
            return upper
        return f"KRW-{upper}"
    return value.upper()


def _normalize_period(period: str, market: str) -> str:
    normalized = str(period or "day").strip().lower()
    allowed = {"day", "week", "month", "1h", "4h"}
    if normalized not in allowed:
        raise ValidationError("period must be one of day/week/month/1h/4h")
    if normalized == "4h" and market != "crypto":
        raise ValidationError("4h period is supported only for crypto")
    return normalized


def _to_candle_rows(
    frame: pd.DataFrame,
    *,
    symbol: str,
    market: str,
    source: str,
    period: str,
) -> list[Candle]:
    if frame.empty:
        return []

    rows: list[Candle] = []
    for _, row in frame.iterrows():
        timestamp_raw = row.get("datetime")
        if timestamp_raw is None:
            date_raw = row.get("date")
            timestamp_raw = pd.to_datetime(date_raw)
        timestamp = pd.Timestamp(timestamp_raw).to_pydatetime()
        rows.append(
            Candle(
                symbol=symbol,
                market=market,
                source=source,
                period=period,
                timestamp=timestamp,
                open=float(row.get("open") or 0.0),
                high=float(row.get("high") or 0.0),
                low=float(row.get("low") or 0.0),
                close=float(row.get("close") or 0.0),
                volume=float(row.get("volume") or 0.0),
                value=(
                    float(row.get("value")) if row.get("value") is not None else None
                ),
            )
        )
    return rows


def _map_error(exc: Exception) -> Exception:
    if isinstance(
        exc,
        (
            ValidationError,
            SymbolNotFoundError,
            RateLimitError,
            UpstreamUnavailableError,
        ),
    ):
        return exc
    if isinstance(exc, RateLimitExceededError):
        return RateLimitError(str(exc))
    text = str(exc)
    if "not found" in text.lower() or "no data" in text.lower():
        return SymbolNotFoundError(text)
    return UpstreamUnavailableError(text)


async def get_kr_volume_rank() -> list[dict[str, Any]]:
    try:
        kis = KISClient()
        rows = await kis.volume_rank()
        return list(rows)
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_quote(symbol: str, market: str) -> Quote:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)

    try:
        if resolved_market == "crypto":
            prices = await fetch_multiple_current_prices([resolved_symbol])
            price = prices.get(resolved_symbol)
            if price is None:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
            return Quote(
                symbol=resolved_symbol,
                market=resolved_market,
                price=float(price),
                source="upbit",
            )

        if resolved_market == "equity_us":
            fast_info = await fetch_fast_info(resolved_symbol)
            close = fast_info.get("close")
            if close is None:
                raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
            return Quote(
                symbol=resolved_symbol,
                market=resolved_market,
                price=float(close),
                source="yahoo",
                previous_close=(
                    float(fast_info["previous_close"])
                    if fast_info.get("previous_close") is not None
                    else None
                ),
                open=(
                    float(fast_info["open"])
                    if fast_info.get("open") is not None
                    else None
                ),
                high=(
                    float(fast_info["high"])
                    if fast_info.get("high") is not None
                    else None
                ),
                low=(
                    float(fast_info["low"])
                    if fast_info.get("low") is not None
                    else None
                ),
                volume=(
                    int(float(fast_info["volume"]))
                    if fast_info.get("volume") is not None
                    else None
                ),
            )

        kis = KISClient()
        frame = await kis.inquire_daily_itemchartprice(
            code=resolved_symbol,
            market="UN",
            n=1,
            period="D",
        )
        if frame.empty:
            raise SymbolNotFoundError(f"Symbol '{resolved_symbol}' not found")
        last = frame.iloc[-1]
        return Quote(
            symbol=resolved_symbol,
            market=resolved_market,
            price=float(last.get("close") or 0.0),
            source="kis",
            open=(float(last["open"]) if last.get("open") is not None else None),
            high=(float(last["high"]) if last.get("high") is not None else None),
            low=(float(last["low"]) if last.get("low") is not None else None),
            volume=(
                int(float(last["volume"])) if last.get("volume") is not None else None
            ),
            value=(float(last["value"]) if last.get("value") is not None else None),
        )
    except Exception as exc:
        raise _map_error(exc) from exc


async def get_ohlcv(
    symbol: str,
    market: str,
    period: str,
    count: int,
    end: dt.datetime | None = None,
) -> list[Candle]:
    resolved_market = _normalize_market(market)
    resolved_symbol = _normalize_symbol(symbol, resolved_market)
    resolved_period = _normalize_period(period, resolved_market)

    if count <= 0:
        raise ValidationError("count must be > 0")

    try:
        if resolved_market == "crypto":
            frame = await fetch_upbit_ohlcv(
                market=resolved_symbol,
                days=min(count, 200),
                period=resolved_period,
                end_date=end,
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="upbit",
                period=resolved_period,
            )

        if resolved_market == "equity_us":
            frame = await fetch_yahoo_ohlcv(
                ticker=resolved_symbol,
                days=min(count, 200),
                period=resolved_period,
                end_date=end,
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="yahoo",
                period=resolved_period,
            )

        kis = KISClient()
        if resolved_period in {"day", "week", "month"}:
            period_map = {"day": "D", "week": "W", "month": "M"}
            frame = await kis.inquire_daily_itemchartprice(
                code=resolved_symbol,
                market="UN",
                n=min(count, 200),
                period=period_map[resolved_period],
                end_date=(end.date() if end is not None else None),
            )
            return _to_candle_rows(
                frame,
                symbol=resolved_symbol,
                market=resolved_market,
                source="kis",
                period=resolved_period,
            )

        frame = await kis.inquire_minute_chart(
            code=resolved_symbol,
            market="UN",
            time_unit=60,
            n=min(count, 200),
            end_date=(end.date() if end is not None else None),
        )
        return _to_candle_rows(
            frame,
            symbol=resolved_symbol,
            market=resolved_market,
            source="kis",
            period=resolved_period,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


__all__ = ["get_quote", "get_ohlcv", "get_kr_volume_rank", "Quote", "Candle"]
