"""MCP analysis tool handlers.

This module contains the MCP callable implementations for analysis and screening tools.
Tool registration itself is handled separately in ``analysis_registration.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from typing import Any, Literal

import httpx
import yfinance as yf

from app.mcp_server.tooling.analysis_screen_core import (
    _normalize_asset_type,
    _normalize_screen_market,
    _normalize_sort_by,
    _normalize_sort_order,
    _screen_crypto,
    _screen_kr,
    _screen_us,
    _validate_screen_filters,
)
from app.mcp_server.tooling.analysis_screening import (
    _analyze_stock_impl,
    _calculate_pearson_correlation,
    _error_payload,
    _get_crypto_rankings,
    _get_us_rankings,
    _map_kr_row,
    _normalize_symbol_input,
    _recommend_stocks_impl,
    _resolve_market_type,
    _to_float,
)
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
)
from app.services.kis import KISClient

logger = logging.getLogger(__name__)

try:
    from app.services.disclosures.dart import list_filings
except ImportError:
    list_filings = None

try:
    from data.disclosures.dart_corp_index import NAME_TO_CORP, prime_index
except ImportError:
    NAME_TO_CORP = {}
    prime_index = None


async def get_stock_name_by_code(code: str) -> str | None:
    try:
        from app.services.krx import get_stock_name_by_code as _get_stock_name_by_code

        return await _get_stock_name_by_code(code)
    except Exception as exc:
        logger.debug(
            "Failed to resolve stock code to Korean name: code=%s, error=%s",
            code,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------


async def get_top_stocks_impl(
    market: str = "kr",
    ranking_type: str = "volume",
    limit: int = 20,
) -> dict[str, Any]:
    market = (market or "").strip().lower()
    ranking_type = (ranking_type or "").strip().lower()
    limit_clamped = max(1, min(limit, 50))

    supported_combinations = {
        ("kr", "volume"),
        ("kr", "market_cap"),
        ("kr", "gainers"),
        ("kr", "losers"),
        ("kr", "foreigners"),
        ("us", "volume"),
        ("us", "market_cap"),
        ("us", "gainers"),
        ("us", "losers"),
        ("crypto", "volume"),
        ("crypto", "gainers"),
        ("crypto", "losers"),
    }

    key = (market, ranking_type)
    if key not in supported_combinations:
        return _error_payload(
            source="validation",
            message=f"Unsupported combination: market={market}, ranking_type={ranking_type}",
            query=f"market={market}, ranking_type={ranking_type}",
        )

    fetch_limit = limit_clamped
    rankings: list[dict[str, Any]] = []
    source = {"kr": "kis", "us": "yfinance", "crypto": "upbit"}.get(
        market,
        "",
    )

    try:
        if market == "kr":
            kis = KISClient()

            if ranking_type == "volume":
                data = await kis.volume_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type == "market_cap":
                data = await kis.market_cap_rank(market="J", limit=fetch_limit)
                source = "kis"
            elif ranking_type in ("gainers", "losers"):
                direction = "up" if ranking_type == "gainers" else "down"
                data = await kis.fluctuation_rank(
                    market="J", direction=direction, limit=fetch_limit
                )
                source = "kis"
            elif ranking_type == "foreigners":
                data = await kis.foreign_buying_rank(market="J", limit=fetch_limit)
                source = "kis"
            else:
                data = []

            filtered_rank = 1
            for row in data[:fetch_limit]:
                if ranking_type == "losers":
                    change_rate = _to_float(row.get("prdy_ctrt"))
                    if change_rate is None or change_rate >= 0:
                        continue

                mapped = _map_kr_row(row, filtered_rank)
                rankings.append(mapped)
                filtered_rank += 1
                if len(rankings) >= limit_clamped:
                    break

        elif market == "us":
            rankings, source = await _get_us_rankings(ranking_type, limit_clamped)

        elif market == "crypto":
            rankings, source = await _get_crypto_rankings(ranking_type, limit_clamped)

        else:
            return _error_payload(
                source="validation",
                message=f"Unsupported market: {market}",
                query=f"market={market}",
            )

    except Exception as exc:
        return _error_payload(
            source=source,
            message=str(exc),
        )

    if len(rankings) == 0 and market == "kr" and ranking_type == "losers":
        return _error_payload(
            source="kis",
            message=(
                "No losing stocks found. "
                "Market may be entirely bullish or KIS API limitation."
            ),
            query="market=kr, ranking_type=losers",
            suggestion=(
                "This could indicate no stocks are declining, "
                "or the KIS API may have limited data for this ranking type."
            ),
        )

    kst_tz = datetime.timezone(datetime.timedelta(hours=9))
    return {
        "rankings": rankings,
        "total_count": len(rankings),
        "market": market,
        "ranking_type": ranking_type,
        "timestamp": datetime.datetime.now(kst_tz).isoformat(),
        "source": source,
    }


async def get_disclosures_impl(
    symbol: str,
    days: int = 30,
    limit: int = 20,
    report_type: str | None = None,
) -> dict[str, Any]:
    if list_filings is None:
        return {
            "success": False,
            "error": "DART functionality not available (dart_fss package not installed)",
        }

    korean_name = symbol.strip()

    if korean_name.isdigit():
        try:
            resolved_name = await get_stock_name_by_code(korean_name)
            if resolved_name:
                korean_name = resolved_name
        except Exception as exc:
            logger.debug(
                "Stock code conversion raised unexpected error: symbol=%s, error=%s",
                symbol,
                exc,
            )
            pass

    if NAME_TO_CORP is not None and not NAME_TO_CORP:
        if prime_index is None:
            return {
                "success": False,
                "error": "DART index not available",
                "symbol": symbol,
            }
        try:
            await prime_index()
        except Exception as exc:
            return {
                "success": False,
                "error": f"Failed to prime DART index: {exc}",
                "symbol": symbol,
            }

    try:
        result = await list_filings(korean_name, days, limit, report_type)
        if isinstance(result, list):
            return {"success": True, "filings": result}
        return result
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "symbol": symbol,
        }


async def get_correlation_impl(
    symbols: list[str],
    period: int = 60,
) -> dict[str, Any]:
    if not symbols or len(symbols) < 2:
        raise ValueError("symbols must contain at least 2 assets")

    if len(symbols) > 10:
        raise ValueError("Maximum 10 symbols supported for correlation calculation")

    period = max(period, 30)
    if period > 365:
        raise ValueError("period must be between 30 and 365 days")

    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    errors: list[str] = []
    price_data: dict[str, list[float]] = {}
    market_types: dict[str, str] = {}

    async def fetch_prices(symbol: str) -> None:
        try:
            market_type, normalized_symbol = _resolve_market_type(symbol, None)
            market_types[normalized_symbol] = market_type

            df = await _fetch_ohlcv_for_indicators(
                normalized_symbol,
                market_type,
                count=period,
            )
            if df.empty:
                raise ValueError(f"No data available for symbol '{symbol}'")
            if "close" not in df.columns:
                raise ValueError(f"Missing close price data for symbol '{symbol}'")

            prices = df["close"].tolist()
            price_data[normalized_symbol] = prices
        except Exception as exc:
            errors.append(f"{symbol}: {str(exc)}")

    await asyncio.gather(*[fetch_prices(sym) for sym in symbols])

    if len(price_data) < 2:
        return {
            "success": False,
            "error": (
                "Insufficient data to calculate correlation (need at least 2 symbols)"
            ),
        }

    correlation_matrix: list[list[float]] = []
    sorted_symbols = sorted(price_data.keys())

    for i, sym_a in enumerate(sorted_symbols):
        row: list[float] = []
        prices_a = price_data[sym_a]
        min_len = len(prices_a)

        for j, sym_b in enumerate(sorted_symbols):
            prices_b = price_data[sym_b]
            actual_len = min(len(prices_b), min_len)

            corr = 0.0
            if i <= j:
                truncated_a = prices_a[-actual_len:]
                truncated_b = prices_b[-actual_len:]
                corr = (
                    _calculate_pearson_correlation(truncated_a, truncated_b)
                    if len(truncated_a) >= 2
                    else 0.0
                )
            else:
                corr = correlation_matrix[j][i]

            row.append(corr)
        correlation_matrix.append(row)

    metadata = {
        "period_days": period,
        "symbols": sorted_symbols,
        "market_types": {
            sym: market_types.get(sym, "unknown") for sym in sorted_symbols
        },
        "sources": {
            sym: source_map.get(market_types.get(sym, "equity_us"), "unknown")
            for sym in sorted_symbols
        },
    }

    if errors:
        return {
            "success": True,
            "correlation_matrix": correlation_matrix,
            "symbols": sorted_symbols,
            "metadata": metadata,
            "errors": errors,
        }

    return {
        "success": True,
        "correlation_matrix": correlation_matrix,
        "symbols": sorted_symbols,
        "metadata": metadata,
    }


async def analyze_stock_impl(
    symbol: str | int,
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    normalized_symbol = _normalize_symbol_input(symbol, market)
    result = _analyze_stock_impl(normalized_symbol, market, include_peers)
    if asyncio.iscoroutine(result):
        return await result
    return result


async def analyze_portfolio_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    if not symbols:
        raise ValueError("symbols must contain at least one entry")

    if len(symbols) > 10:
        raise ValueError("symbols must contain at most 10 entries")

    normalized_symbols = [_normalize_symbol_input(s, market) for s in symbols]
    results: dict[str, Any] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(5)

    async def _analyze_one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                result = _analyze_stock_impl(sym, market, include_peers)
                if asyncio.iscoroutine(result):
                    return await result
                return result
            except Exception as exc:
                errors.append(f"{sym}: {str(exc)}")
                return {"symbol": sym, "error": str(exc)}

    analyze_results = await asyncio.gather(
        *[_analyze_one(s) for s in normalized_symbols]
    )

    success_count = 0
    fail_count = 0
    for sym, result in zip(normalized_symbols, analyze_results, strict=True):
        results[sym] = result
        if "error" not in result:
            success_count += 1
        else:
            fail_count += 1

    return {
        "results": results,
        "summary": {
            "total_symbols": len(normalized_symbols),
            "successful": success_count,
            "failed": fail_count,
            "errors": errors,
        },
    }


async def screen_stocks_impl(
    market: Literal["kr", "kospi", "kosdaq", "us", "crypto"] = "kr",
    asset_type: Literal["stock", "etf", "etn"] | None = None,
    category: str | None = None,
    strategy: str | None = None,
    sort_by: Literal[
        "volume", "market_cap", "change_rate", "dividend_yield", "rsi"
    ] = "volume",
    sort_order: Literal["asc", "desc"] = "desc",
    min_market_cap: float | None = None,
    max_per: float | None = None,
    max_pbr: float | None = None,
    min_dividend_yield: float | None = None,
    max_rsi: float | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    strategy_presets: dict[str, dict[str, Any]] = {
        "oversold": {"max_rsi": 30.0, "sort_by": "volume", "sort_order": "desc"},
        "momentum": {"sort_by": "change_rate", "sort_order": "desc"},
        "high_volume": {"sort_by": "volume", "sort_order": "desc"},
    }
    if strategy:
        strategy_key = strategy.strip().lower()
        if strategy_key not in strategy_presets:
            valid = ", ".join(sorted(strategy_presets.keys()))
            raise ValueError(f"screen strategy must be one of: {valid}")
        preset = strategy_presets[strategy_key]
        sort_by = preset.get("sort_by", sort_by)
        sort_order = preset.get("sort_order", sort_order)
        if max_rsi is None and "max_rsi" in preset:
            max_rsi = preset["max_rsi"]

    normalized_market = _normalize_screen_market(market)
    normalized_asset_type = _normalize_asset_type(asset_type)
    normalized_sort_by = _normalize_sort_by(sort_by)
    normalized_sort_order = _normalize_sort_order(sort_order)

    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > 50:
        limit = 50

    _validate_screen_filters(
        market=normalized_market,
        asset_type=normalized_asset_type,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=min_dividend_yield,
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
    )

    if normalized_market in ("kr", "kospi", "kosdaq"):
        return await _screen_kr(
            market=normalized_market,
            asset_type=normalized_asset_type,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            max_pbr=max_pbr,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
            limit=limit,
        )
    if normalized_market == "us":
        return await _screen_us(
            market=normalized_market,
            asset_type=normalized_asset_type,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
            limit=limit,
        )
    if normalized_market == "crypto":
        # Intentional for this scope: keep crypto enrichment call policy unchanged.
        # 429/distributed rate-limit optimization is handled as a follow-up task.
        return await _screen_crypto(
            market=normalized_market,
            asset_type=normalized_asset_type,
            category=category,
            min_market_cap=min_market_cap,
            max_per=max_per,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=normalized_sort_by,
            sort_order=normalized_sort_order,
            limit=limit,
            enrich_rsi=True,
        )

    return _error_payload(
        source="screen_stocks",
        message=f"Unsupported market: {normalized_market}",
    )


async def recommend_stocks_impl(
    budget: float,
    market: str = "kr",
    strategy: str = "balanced",
    exclude_symbols: list[str] | None = None,
    sectors: list[str] | None = None,
    max_positions: int = 5,
    exclude_held: bool = True,
) -> dict[str, Any]:
    return await _recommend_stocks_impl(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        exclude_held=exclude_held,
        top_stocks_fallback=get_top_stocks_impl,
    )


async def get_dividends_impl(symbol: str) -> dict[str, Any]:
    symbol = (symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required")

    ticker = yf.Ticker(symbol.upper())

    def fetch_sync() -> dict[str, Any]:
        try:
            info = ticker.info or {}

            dividend_yield = info.get("dividendYield")
            dividend_rate = info.get("dividendRate")
            ex_date = info.get("exDividendDate")

            divs = ticker.dividends
            last_div = None
            if divs is not None and not divs.empty:
                last_date = divs.index[-1]
                last_div = {
                    "date": last_date.strftime("%Y-%m-%d"),
                    "amount": float(divs.iloc[-1]),
                }

            return {
                "success": True,
                "symbol": symbol.upper(),
                "dividend_yield": round(dividend_yield, 4) if dividend_yield else None,
                "dividend_rate": float(dividend_rate) if dividend_rate else None,
                "ex_dividend_date": (
                    datetime.datetime.fromtimestamp(ex_date).strftime("%Y-%m-%d")
                    if ex_date
                    else None
                ),
                "last_dividend": last_div,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "symbol": symbol.upper(),
            }

    return await asyncio.to_thread(fetch_sync)


async def get_fear_greed_index_impl(days: int = 7) -> dict[str, Any]:
    capped_days = min(max(days, 1), 365)
    url = "https://api.alternative.me/fng/"
    params = {"limit": capped_days}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()

        if not data or "data" not in data:
            return _error_payload(
                source="alternative.me",
                message="No data received from Fear & Greed API",
            )

        history_data = data["data"]
        if not history_data:
            return _error_payload(
                source="alternative.me",
                message="Empty data array received from Fear & Greed API",
            )

        current = history_data[0]
        current_value = int(current["value"])
        current_classification = current["value_classification"]
        current_timestamp = current["timestamp"]
        current_date = (
            datetime.datetime.fromtimestamp(int(current_timestamp)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            if current_timestamp
            else "Unknown"
        )

        history = []
        for item in history_data:
            value = int(item["value"])
            classification = item["value_classification"]
            timestamp = item["timestamp"]
            date = (
                datetime.datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d")
                if timestamp
                else "Unknown"
            )
            history.append(
                {"date": date, "value": value, "classification": classification}
            )

        return {
            "success": True,
            "source": "alternative.me",
            "current": {
                "value": current_value,
                "classification": current_classification,
                "date": current_date,
            },
            "history": history,
        }

    except httpx.HTTPStatusError as exc:
        return _error_payload(
            source="alternative.me",
            message=f"HTTP error: {exc}",
        )
    except Exception as exc:
        return _error_payload(
            source="alternative.me",
            message=str(exc),
        )


__all__ = [
    "analyze_portfolio_impl",
    "analyze_stock_impl",
    "get_correlation_impl",
    "get_disclosures_impl",
    "get_dividends_impl",
    "get_fear_greed_index_impl",
    "get_top_stocks_impl",
    "recommend_stocks_impl",
    "screen_stocks_impl",
]
