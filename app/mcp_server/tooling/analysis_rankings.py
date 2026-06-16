"""Ranking/correlation helpers extracted from analysis_screening."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import yfinance as yf

import app.services.brokers.upbit.client as upbit_service
from app.monitoring import yfinance_tracing_session


async def get_us_rankings_impl(
    ranking_type: str,
    limit: int,
    map_us_row: Callable[[dict[str, Any], int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    screener_ids = {
        "volume": "most_actives",
        "gainers": "day_gainers",
        "losers": "day_losers",
    }

    screener_id = screener_ids.get(ranking_type)

    def fetch_sync(session):
        if ranking_type == "market_cap":
            query = yf.EquityQuery(
                "and",
                [
                    yf.EquityQuery("eq", ["region", "us"]),
                    yf.EquityQuery("gte", ["intradaymarketcap", 2000000000]),
                    yf.EquityQuery("gte", ["intradayprice", 5]),
                    yf.EquityQuery("gt", ["dayvolume", 15000]),
                ],
            )
            return yf.screen(
                query,
                size=limit,
                sortField="intradaymarketcap",
                sortAsc=False,
                session=session,
            )
        return yf.screen(screener_id, session=session)

    with yfinance_tracing_session() as session:
        results = await asyncio.to_thread(fetch_sync, session)

    temp_rankings: list[dict[str, Any]] = []
    if isinstance(results, dict):
        quotes = results.get("quotes", [])
        if not quotes:
            raise RuntimeError(
                f"Empty quotes response for ranking_type='{ranking_type}' from yfinance"
            )
        for row in quotes[:limit]:
            if ranking_type == "losers":
                price = row.get("regularMarketPrice", 0)
                prev_close = row.get("previousClose", 0)
                if prev_close and price >= prev_close:
                    continue
            temp_rankings.append(row)
    else:
        if results.empty:
            raise RuntimeError(
                f"Empty DataFrame response for ranking_type='{ranking_type}' from yfinance"
            )
        for row in results.head(limit).to_dict(orient="records"):
            if ranking_type == "losers":
                price = row.get("regularMarketPrice", 0)
                prev_close = row.get("previousClose", 0)
                if prev_close and price >= prev_close:
                    continue
            temp_rankings.append(row)

    if ranking_type == "losers" and temp_rankings:
        temp_rankings.sort(
            key=lambda x: (
                (x.get("regularMarketPrice", 0) - x.get("previousClose", 0))
                / x.get("previousClose", 1)
            )
        )

    rankings = [map_us_row(row, i) for i, row in enumerate(temp_rankings, 1)]
    return rankings, "yfinance"


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _with_crypto_relative_strength(coins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    btc_rate = None
    for coin in coins:
        if str(coin.get("market") or "").upper() == "KRW-BTC":
            btc_rate = _as_float(coin.get("signed_change_rate"))
            break
    if btc_rate is None:
        return []

    rows: list[dict[str, Any]] = []
    for coin in coins:
        market = str(coin.get("market") or "").upper()
        if market == "KRW-BTC":
            continue
        rate = _as_float(coin.get("signed_change_rate"))
        if rate is None:
            continue
        relative = rate - btc_rate
        rows.append(
            {
                **coin,
                "relative_strength_vs_btc_24h": round(relative, 8),
                "relative_strength_pct_vs_btc_24h": round(relative * 100, 4),
                "btc_change_rate_24h": btc_rate,
            }
        )
    rows.sort(
        key=lambda row: (
            row["relative_strength_vs_btc_24h"],
            _as_float(row.get("acc_trade_price_24h")) or 0.0,
            str(row.get("market") or ""),
        ),
        reverse=True,
    )
    return rows


async def get_crypto_rankings_impl(
    ranking_type: str,
    limit: int,
    map_crypto_row: Callable[[dict[str, Any], int], dict[str, Any]],
) -> tuple[list[dict[str, Any]], str]:
    coins = await upbit_service.fetch_top_traded_coins()

    if ranking_type == "volume":
        sorted_coins = coins
    elif ranking_type == "gainers":
        sorted_coins = sorted(
            coins, key=lambda x: float(x.get("signed_change_rate", 0)), reverse=True
        )
    elif ranking_type == "losers":
        negative_coins = [c for c in coins if float(c.get("signed_change_rate", 0)) < 0]
        sorted_coins = sorted(
            negative_coins, key=lambda x: float(x.get("signed_change_rate", 0))
        )
    elif ranking_type == "relative_strength":
        sorted_coins = _with_crypto_relative_strength(coins)
    else:
        sorted_coins = coins

    rankings = [
        map_crypto_row(coin, i) for i, coin in enumerate(sorted_coins[:limit], 1)
    ]
    return rankings, "upbit"


def calculate_pearson_correlation(x: list[float], y: list[float]) -> float:
    """Calculate Pearson correlation coefficient between two lists."""
    n = len(x)
    if n != len(y) or n < 2:
        return 0.0

    sum_x = sum(x)
    sum_y = sum(y)
    sum_xy = sum(xi * yi for xi, yi in zip(x, y, strict=True))
    sum_x2 = sum(xi**2 for xi in x)
    sum_y2 = sum(yi**2 for yi in y)

    numerator = n * sum_xy - sum_x * sum_y
    denominator_x = n * sum_x2 - sum_x**2
    denominator_y = n * sum_y2 - sum_y**2
    denominator = (denominator_x * denominator_y) ** 0.5

    if denominator == 0:
        return 0.0

    return numerator / denominator


__all__ = [
    "get_us_rankings_impl",
    "get_crypto_rankings_impl",
    "calculate_pearson_correlation",
]
