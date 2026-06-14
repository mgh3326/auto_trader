"""ROB-265 Plan 4 — stateless market-data helpers for the watch scanners.

Extracted from the legacy :class:`app.jobs.watch_scanner.WatchScanner`
so the new :class:`InvestmentWatchScanner` can read live price /
RSI / trade-value / index / FX values without inheriting from the
legacy class (which couples to AgentGatewayClient).

Behaviourally identical to the legacy methods. Plan 5 deletes the
legacy scanner's duplicate copy.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Any

import exchange_calendars as xcals
import pandas as pd
from pandas import Timestamp

from app.mcp_server.tooling.market_data_indicators import _calculate_rsi
from app.services import exchange_rate_service, market_index_service
from app.services import market_data as market_data_service

_CRYPTO_RSI_LOOKBACK_DAYS = 200


@lru_cache(maxsize=2)
def _get_calendar(market: str):
    if market == "kr":
        return xcals.get_calendar("XKRX")
    if market == "us":
        return xcals.get_calendar("XNYS")
    return None


def is_market_open(market: str) -> bool:
    if market == "crypto":
        return True

    calendar = _get_calendar(market)
    if calendar is None:
        return False

    now_utc = Timestamp.now("UTC").floor("min")
    if now_utc.tz is None:
        now_utc = now_utc.tz_localize("UTC")
    now_in_market_tz = now_utc.tz_convert(calendar.tz)
    return bool(calendar.is_trading_minute(now_in_market_tz))


def _to_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_crypto_symbol(symbol: str) -> str:
    upper_symbol = symbol.strip().upper()
    if "-" in upper_symbol:
        return upper_symbol
    return f"KRW-{upper_symbol}"


async def get_price(symbol: str, market: str) -> float | None:
    if market == "crypto":
        quote = await market_data_service.get_quote(
            symbol=normalize_crypto_symbol(symbol),
            market="crypto",
        )
        return _to_float(getattr(quote, "price", None))
    if market == "kr":
        quote = await market_data_service.get_quote(
            symbol=symbol,
            market="equity_kr",
        )
        return _to_float(getattr(quote, "price", None))
    if market == "us":
        normalized_symbol = str(symbol or "").strip().upper()
        quote = await market_data_service.get_quote(
            symbol=normalized_symbol,
            market="equity_us",
        )
        price = _to_float(getattr(quote, "price", None))
        if price is None:
            raise ValueError(
                f"US watch price fetch failed for {normalized_symbol}: invalid close"
            )
        return price
    return None


async def get_trade_value(symbol: str, market: str) -> float | None:
    if market != "kr":
        return None
    quote = await market_data_service.get_quote(
        symbol=symbol,
        market="equity_kr",
    )
    return _to_float(getattr(quote, "value", None))


async def get_index_price(symbol: str, market: str) -> float | None:
    if market != "kr":
        return None
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol not in {"KOSPI", "KOSDAQ"}:
        return None
    data = await market_index_service.get_kr_index_quote(normalized_symbol)
    return _to_float(data.get("current"))


async def get_fx_price(symbol: str) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol != "USDKRW":
        return None
    return _to_float(await exchange_rate_service.get_usd_krw_quote())


async def get_rsi(symbol: str, market: str) -> float | None:
    if market == "crypto":
        symbol_for_query = normalize_crypto_symbol(symbol)
        market_for_query = "crypto"
        count = _CRYPTO_RSI_LOOKBACK_DAYS
    elif market == "kr":
        symbol_for_query = symbol
        market_for_query = "equity_kr"
        count = 250
    elif market == "us":
        symbol_for_query = symbol
        market_for_query = "equity_us"
        count = 250
    else:
        return None

    candles = await market_data_service.get_ohlcv(
        symbol=symbol_for_query,
        market=market_for_query,
        period="day",
        count=count,
    )

    if not candles:
        return None

    close_values: list[float] = []
    for candle in candles:
        if isinstance(candle, dict):
            close_raw = candle.get("close")
        else:
            close_raw = getattr(candle, "close", None)
        close_value = _to_float(close_raw)
        if close_value is None:
            continue
        close_values.append(close_value)

    if not close_values:
        return None

    close = pd.Series(close_values, dtype="float64").dropna()
    if close.empty:
        return None

    return _to_float(_calculate_rsi(close).get("14"))


async def get_current_value(
    *,
    target_kind: str,
    metric: str,
    symbol: str,
    market: str,
) -> float | None:
    """Dispatch by ``target_kind`` × ``metric`` to the right value fetch.

    Mirrors the legacy scanner's matrix so RSI / index / FX paths
    are not lost on the new scanner.
    """
    if target_kind == "asset":
        if metric == "price":
            return await get_price(symbol, market)
        if metric == "rsi":
            return await get_rsi(symbol, market)
        if metric == "trade_value":
            return await get_trade_value(symbol, market)
        return None
    if target_kind == "index":
        if metric == "price":
            return await get_index_price(symbol, market)
        return None
    if target_kind == "fx":
        if metric == "price":
            return await get_fx_price(symbol)
        return None
    return None


def is_triggered(current: float | None, operator: str, threshold: float) -> bool:
    if current is None:
        return False
    if operator == "above":
        return current > threshold
    if operator == "below":
        return current < threshold
    return False


def evaluate_clause(current: float | None, clause: dict) -> bool:
    """Evaluate one condition clause against a current value."""
    if current is None:
        return False
    op = clause.get("op")
    if op == "above":
        return current > float(clause["threshold"])
    if op == "below":
        return current < float(clause["threshold"])
    if op == "between":
        return float(clause["low"]) <= current <= float(clause["high"])
    return False


async def evaluate_alert_conditions(
    *,
    target_kind: str,
    symbol: str,
    market: str,
    conditions: list[dict],
    combine: str,
    get_value_fn: Callable = get_current_value,
) -> tuple[bool, float | None]:
    """Evaluate normalized conditions. Returns (triggered, primary_value).

    primary_value is the first clause's current value (used for event detail).
    All clauses share the alert's target_kind/symbol/market; only metric varies.
    """
    primary_value: float | None = None
    results: list[bool] = []
    for idx, clause in enumerate(conditions):
        value = await get_value_fn(
            target_kind=target_kind,
            metric=clause["metric"],
            symbol=symbol,
            market=market,
        )
        if idx == 0:
            primary_value = value
        results.append(evaluate_clause(value, clause))
    triggered = bool(results) and all(results)  # combine == "and"
    return triggered, primary_value


__all__ = [
    "evaluate_alert_conditions",
    "evaluate_clause",
    "get_current_value",
    "get_fx_price",
    "get_index_price",
    "get_price",
    "get_rsi",
    "get_trade_value",
    "is_market_open",
    "is_triggered",
    "normalize_crypto_symbol",
]


def _build_scanner_snapshot(
    *,
    current_value: Any,
    threshold: Any,
    metric: str,
    operator: str,
) -> dict[str, Any]:
    """Return the minimal scanner_snapshot JSONB body for an event row."""
    return {
        "metric": metric,
        "operator": operator,
        "current_value": _to_float(current_value),
        "threshold": _to_float(threshold),
    }
