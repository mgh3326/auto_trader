"""MCP analysis tool handlers.

This module contains the MCP callable implementations for analysis and screening tools.
Tool registration itself is handled separately in ``analysis_registration.py``.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Callable
from typing import Any, Literal

import httpx
import yfinance as yf

from app.mcp_server.tooling import analysis_screening
from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.monitoring import build_yfinance_tracing_session
from app.services.brokers.kis.client import KISClient

logger = logging.getLogger(__name__)

_CORRELATION_COMPANY_NAME_ERROR = (
    "get_correlation does not support company-name inputs because it has no "
    "market parameter. Use ticker/code inputs directly."
)


def _looks_like_correlation_company_name(symbol: str) -> bool:
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, None)
    if not normalized_symbol:
        return False
    if _is_crypto_market(normalized_symbol):
        return False
    if _is_korean_equity_code(normalized_symbol):
        return False
    stripped_symbol = normalized_symbol.strip()
    if any(ch.isspace() for ch in stripped_symbol):
        return True
    if not stripped_symbol.isascii():
        return True
    return False


def _resolve_correlation_symbol_input(symbol: str | int) -> tuple[str, str]:
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, None)
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if _looks_like_correlation_company_name(normalized_symbol):
        raise ValueError(_CORRELATION_COMPANY_NAME_ERROR)
    return analysis_screening._resolve_market_type(normalized_symbol, None)


try:
    from app.services.disclosures.dart import list_filings
except ImportError:
    list_filings = None


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
        return analysis_screening._error_payload(
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
                    change_rate = analysis_screening._to_float(row.get("prdy_ctrt"))
                    if change_rate is None or change_rate >= 0:
                        continue

                mapped = analysis_screening._map_kr_row(row, filtered_rank)
                rankings.append(mapped)
                filtered_rank += 1
                if len(rankings) >= limit_clamped:
                    break

        elif market == "us":
            rankings, source = await analysis_screening._get_us_rankings(
                ranking_type, limit_clamped
            )

        elif market == "crypto":
            rankings, source = await analysis_screening._get_crypto_rankings(
                ranking_type, limit_clamped
            )

        else:
            return analysis_screening._error_payload(
                source="validation",
                message=f"Unsupported market: {market}",
                query=f"market={market}",
            )

    except Exception as exc:
        return analysis_screening._error_payload(
            source=source,
            message=str(exc),
        )

    if len(rankings) == 0 and market == "kr" and ranking_type == "losers":
        return analysis_screening._error_payload(
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
            "error": "DART functionality not available",
            "filings": [],
            "symbol": symbol,
        }

    try:
        return await list_filings(symbol, days, limit, report_type)
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "filings": [],
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
    company_name_validation_errors: list[str] = []
    price_data: dict[str, list[float]] = {}
    market_types: dict[str, str] = {}

    async def fetch_prices(
        symbol: str,
    ) -> tuple[str | None, str | None, list[float] | None, str | None, bool]:
        try:
            market_type, normalized_symbol = _resolve_correlation_symbol_input(symbol)
        except Exception as exc:
            return (
                None,
                None,
                None,
                f"{symbol}: {str(exc)}",
                str(exc) == _CORRELATION_COMPANY_NAME_ERROR,
            )

        try:
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
            return normalized_symbol, market_type, prices, None, False
        except Exception as exc:
            return None, None, None, f"{symbol}: {str(exc)}", False

    results = await asyncio.gather(*[fetch_prices(sym) for sym in symbols])

    for normalized_symbol, market_type, prices, error, is_validation_error in results:
        if error is not None:
            errors.append(error)
            if is_validation_error:
                company_name_validation_errors.append(error)
            continue
        if normalized_symbol is None or market_type is None or prices is None:
            continue
        market_types[normalized_symbol] = market_type
        price_data[normalized_symbol] = prices

    if len(price_data) < 2:
        if company_name_validation_errors:
            return {
                "success": False,
                "error": _CORRELATION_COMPANY_NAME_ERROR,
                "errors": errors,
            }
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
                    analysis_screening._calculate_pearson_correlation(
                        truncated_a, truncated_b
                    )
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
    normalized_symbol = analysis_screening._normalize_symbol_input(symbol, market)
    result = analysis_screening._analyze_stock_impl(
        normalized_symbol, market, include_peers
    )
    if asyncio.iscoroutine(result):
        return await result
    return result


async def _run_batch_analysis(
    symbols: list[str | int],
    *,
    market: str | None,
    include_peers: bool,
    formatter: Callable[[str, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Shared batch analysis executor for portfolio and stock batch analysis.

    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        formatter: Callable that receives (normalized_symbol, analysis_result) and returns formatted result

    Returns:
        Dict with 'results' (symbol -> formatted_result) and 'summary' keys

    Raises:
        ValueError: If symbols list is empty or exceeds 10 entries
    """
    if not symbols:
        raise ValueError("symbols must contain at least one entry")

    if len(symbols) > 10:
        raise ValueError("symbols must contain at most 10 entries")

    normalized_symbols = [
        analysis_screening._normalize_symbol_input(s, market) for s in symbols
    ]
    results: dict[str, Any] = {}
    errors: list[str] = []
    sem = asyncio.Semaphore(5)

    async def _analyze_one(sym: str) -> dict[str, Any]:
        async with sem:
            try:
                result = analysis_screening._analyze_stock_impl(
                    sym, market, include_peers
                )
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
        formatted_result = formatter(sym, result)
        results[sym] = formatted_result
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


def _summarize_analysis_result(
    symbol: str,
    analysis: dict[str, Any],
) -> dict[str, Any]:
    """Convert full analysis into compact summary for batch responses."""
    # If result is an error, pass through unchanged
    if "error" in analysis:
        return analysis

    quote = analysis.get("quote") or {}
    indicators = (analysis.get("indicators") or {}).get("indicators", {})
    rsi = (indicators.get("rsi") or {}).get("14")
    sr = analysis.get("support_resistance") or {}

    return {
        "symbol": symbol,
        "market_type": analysis.get("market_type"),
        "source": analysis.get("source"),
        "current_price": quote.get("price") or quote.get("current_price"),
        "rsi_14": rsi,
        "consensus": ((analysis.get("opinions") or {}).get("consensus")),
        "recommendation": analysis.get("recommendation"),
        "supports": (sr.get("supports") or [])[:3],
        "resistances": (sr.get("resistances") or [])[:3],
    }


async def analyze_stock_batch_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
    quick: bool = True,
) -> dict[str, Any]:
    """Analyze multiple symbols and return compact per-symbol summaries.
    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis
        quick: If True, return compact summary; if False, return full analysis
    Returns:
        Dict with 'results' (symbol -> summary) and 'summary' keys
    """
    formatter = _summarize_analysis_result if quick else (lambda _sym, result: result)
    return await _run_batch_analysis(
        symbols,
        market=market,
        include_peers=include_peers,
        formatter=formatter,
    )


async def analyze_portfolio_impl(
    symbols: list[str | int],
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    """Analyze a portfolio of symbols.

    Args:
        symbols: List of symbol inputs (1-10 entries)
        market: Optional market override
        include_peers: Whether to include peer analysis

    Returns:
        Dict with 'results' (symbol -> analysis_result) and 'summary' keys
    """
    return await _run_batch_analysis(
        symbols,
        market=market,
        include_peers=include_peers,
        formatter=lambda _sym, result: result,
    )


async def screen_stocks_impl(
    market: Literal["kr", "kospi", "kosdaq", "us", "crypto"] = "kr",
    asset_type: Literal["stock", "etf", "etn"] | None = None,
    category: str | None = None,
    sector: str | None = None,
    strategy: str | None = None,
    sort_by: Literal[
        "volume",
        "trade_amount",
        "market_cap",
        "change_rate",
        "dividend_yield",
        "rsi",
    ]
    | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    min_market_cap: float | None = None,
    max_per: float | None = None,
    max_pbr: float | None = None,
    min_dividend_yield: float | None = None,
    min_dividend: float | None = None,
    min_analyst_buy: float | None = None,
    max_rsi: float | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    sort_by_specified = sort_by is not None
    strategy_applied = False

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
        strategy_applied = True
        sort_by = preset.get("sort_by", sort_by)
        sort_order = preset.get("sort_order", sort_order)
        if max_rsi is None and "max_rsi" in preset:
            max_rsi = preset["max_rsi"]

    normalized_request = normalize_screen_request(
        market=market,
        asset_type=asset_type,
        category=category,
        sector=sector,
        strategy=strategy,
        sort_by=sort_by,
        sort_order=sort_order,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        min_dividend=min_dividend,
        min_analyst_buy=min_analyst_buy,
        max_rsi=max_rsi,
        limit=limit,
    )

    normalized_market = analysis_screening._normalize_screen_market(market)
    normalized_asset_type = analysis_screening._normalize_asset_type(asset_type)
    normalized_sort_by = analysis_screening._normalize_sort_by(sort_by)
    normalized_sort_order = analysis_screening._normalize_sort_order(sort_order)

    if normalized_market == "crypto" and not sort_by_specified:
        if strategy_applied:
            if normalized_sort_by == "volume":
                normalized_sort_by = "trade_amount"
        else:
            normalized_sort_by = "rsi"
            normalized_sort_order = "asc"

    if limit < 1:
        raise ValueError("limit must be at least 1")
    if limit > 100:
        limit = 100

    analysis_screening._validate_screen_filters(
        market=normalized_market,
        asset_type=normalized_asset_type,
        min_market_cap=min_market_cap,
        max_per=max_per,
        min_dividend_yield=normalized_request["min_dividend_yield"],
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
    )
    # Use unified screening with automatic data source selection
    return await analysis_screening.screen_stocks_unified(
        market=normalized_market,
        asset_type=normalized_asset_type,
        category=category,
        sector=sector,
        min_market_cap=min_market_cap,
        max_per=max_per,
        max_pbr=max_pbr,
        min_dividend_yield=min_dividend_yield,
        min_dividend=min_dividend,
        min_analyst_buy=min_analyst_buy,
        max_rsi=max_rsi,
        sort_by=normalized_sort_by,
        sort_order=normalized_sort_order,
        limit=limit,
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
    return await analysis_screening._recommend_stocks_impl(
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

    session = build_yfinance_tracing_session()
    ticker = yf.Ticker(symbol.upper(), session=session)

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
            return analysis_screening._error_payload(
                source="alternative.me",
                message="No data received from Fear & Greed API",
            )

        history_data = data["data"]
        if not history_data:
            return analysis_screening._error_payload(
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
        return analysis_screening._error_payload(
            source="alternative.me",
            message=f"HTTP error: {exc}",
        )
    except Exception as exc:
        return analysis_screening._error_payload(
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
