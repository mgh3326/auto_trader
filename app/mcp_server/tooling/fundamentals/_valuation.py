"""Handlers for valuation and equity-analysis tools.

Includes: get_valuation, get_investment_opinions, get_investor_trends, get_short_interest.
"""

from __future__ import annotations

import datetime
from collections import defaultdict
from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_equity_market
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_investment_opinions_naver,
    _fetch_investor_trends_naver,
    _fetch_valuation_naver,
)
from app.mcp_server.tooling.fundamentals_sources_yfinance import (
    _fetch_investment_opinions_yfinance,
    _fetch_valuation_yfinance,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.services import market_data as market_data_service


async def handle_get_valuation(
    symbol: str | int,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Valuation metrics are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    normalized_market = normalize_equity_market(market)

    try:
        if normalized_market == "kr":
            return await _fetch_valuation_naver(symbol)
        return await _fetch_valuation_yfinance(symbol)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_investment_opinions(
    symbol: str | int,
    limit: int = 10,
    market: str | None = None,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if _is_crypto_market(symbol):
        raise ValueError("Investment opinions are not available for cryptocurrencies")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        else:
            market = "us"

    if not market:
        raise ValueError("market is required")

    normalized_market = normalize_equity_market(str(market))
    capped_limit = min(max(limit, 1), 30)

    try:
        if normalized_market == "kr":
            return await _fetch_investment_opinions_naver(symbol, capped_limit)
        return await _fetch_investment_opinions_yfinance(symbol, capped_limit)
    except Exception as exc:
        source = "naver" if normalized_market == "kr" else "yfinance"
        instrument_type = "equity_kr" if normalized_market == "kr" else "equity_us"
        return _error_payload(
            source=source,
            message=str(exc),
            symbol=symbol,
            instrument_type=instrument_type,
        )


async def handle_get_investor_trends(
    symbol: str,
    days: int = 20,
    period: str = "day",
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Investor trends are only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    period = (period or "day").lower()
    if period not in ("day", "week", "month"):
        raise ValueError("period must be 'day', 'week', or 'month'")

    # Fetch more daily data when aggregating to week/month
    if period == "month":
        fetch_days = min(max(days, 1), 60) * 22  # ~22 trading days/month
    elif period == "week":
        fetch_days = min(max(days, 1), 60) * 5
    else:
        fetch_days = min(max(days, 1), 60)
    fetch_days = min(fetch_days, 60)  # Naver caps at ~60 rows per page

    try:
        result = await _fetch_investor_trends_naver(symbol, fetch_days)
    except Exception as exc:
        return _error_payload(
            source="naver",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )

    # Add individual_net (derived: negative of institutional + foreign)
    for row in result.get("data", []):
        inst = row.get("institutional_net") or 0
        frgn = row.get("foreign_net") or 0
        row["individual_net"] = -(inst + frgn)

    if period != "day":
        result["data"] = _aggregate_investor_data(result["data"], period)

    result["period"] = period
    capped_days = min(max(days, 1), 60)
    result["data"] = result["data"][:capped_days]
    result["days"] = len(result["data"])
    return result


def _aggregate_investor_data(
    daily_data: list[dict[str, Any]], period: str
) -> list[dict[str, Any]]:
    """Aggregate daily investor flow data into weekly or monthly buckets."""
    if not daily_data:
        return []

    buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for row in daily_data:
        date_str = row.get("date", "")
        if not date_str:
            continue
        try:
            dt = datetime.date.fromisoformat(date_str)
        except (ValueError, TypeError):
            continue

        if period == "week":
            # ISO week: Monday-based
            iso = dt.isocalendar()
            key = f"{iso[0]}-W{iso[1]:02d}"
        else:  # month
            key = f"{dt.year}-{dt.month:02d}"

        buckets[key].append(row)

    aggregated: list[dict[str, Any]] = []
    for key in sorted(buckets, reverse=True):
        rows = buckets[key]
        # Use the most recent date in the bucket as representative
        rows_sorted = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)
        agg: dict[str, Any] = {
            "period_key": key,
            "date_start": rows_sorted[-1].get("date", ""),
            "date_end": rows_sorted[0].get("date", ""),
            "trading_days": len(rows),
            "close": rows_sorted[0].get("close"),
            "volume": sum(r.get("volume") or 0 for r in rows),
            "institutional_net": sum(r.get("institutional_net") or 0 for r in rows),
            "foreign_net": sum(r.get("foreign_net") or 0 for r in rows),
            "individual_net": sum(r.get("individual_net") or 0 for r in rows),
        }
        aggregated.append(agg)

    return aggregated


async def handle_get_short_interest(
    symbol: str,
    days: int = 20,
) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    if not _is_korean_equity_code(symbol):
        raise ValueError(
            "Short selling data is only available for Korean stocks "
            "(6-digit codes like '005930')"
        )

    capped_days = min(max(days, 1), 60)

    try:
        return await market_data_service.get_short_interest(symbol, capped_days)
    except Exception as exc:
        return _error_payload(
            source="kis",
            message=str(exc),
            symbol=symbol,
            instrument_type="equity_kr",
        )
