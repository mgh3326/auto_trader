"""Analysis and screening MCP tool helper implementations."""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp_server.tooling.analysis_rankings import (
    calculate_pearson_correlation as _calculate_pearson_correlation_impl,
)
from app.mcp_server.tooling.analysis_rankings import (
    get_crypto_rankings_impl as _get_crypto_rankings_impl,
)
from app.mcp_server.tooling.analysis_rankings import (
    get_us_rankings_impl as _get_us_rankings_impl,
)
from app.mcp_server.tooling.analysis_recommend import (
    recommend_stocks_impl as _recommend_stocks_impl_core,
)
from app.mcp_server.tooling.analysis_screen_core import (
    _screen_crypto,
    _screen_kr,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_company_profile_finnhub,
    _fetch_investment_opinions_naver,
    _fetch_investment_opinions_yfinance,
    _fetch_news_finnhub,
    _fetch_news_naver,
    _fetch_sector_peers_naver,
    _fetch_sector_peers_us,
    _fetch_valuation_naver,
    _fetch_valuation_yfinance,
)
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import (
    _build_recommendation_for_equity,
    _normalize_symbol_input,
    _resolve_market_type,
    _to_float,
    _to_int,
    _to_optional_float,
)
from app.mcp_server.tooling.shared import (
    _error_payload as _error_payload_impl,
)


def _error_payload(
    source: str, message: str, **kwargs: Any,
) -> dict[str, Any]:
    return _error_payload_impl(source=source, message=message, **kwargs)

# ---------------------------------------------------------------------------
# Change Rate Normalization
# ---------------------------------------------------------------------------


def _parse_change_rate(value: Any) -> float | None:
    val = _to_optional_float(value)
    if val is None:
        return None
    return val


def _normalize_change_rate_equity(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val


def _normalize_change_rate_crypto(value: Any) -> float:
    val = _parse_change_rate(value)
    if val is None:
        return 0.0
    return val * 100


# ---------------------------------------------------------------------------
# Ranking Row Mapping
# ---------------------------------------------------------------------------


def _map_kr_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("stck_shrn_iscd") or row.get("mksc_shrn_iscd", "")
    name = row.get("hts_kor_isnm", "")
    price = _to_float(row.get("stck_prpr"))
    change_rate = _normalize_change_rate_equity(row.get("prdy_ctrt"))
    volume = _to_int(row.get("acml_vol") or row.get("frgn_ntby_qty"))
    market_cap = _to_float(row.get("hts_avls"))
    trade_amount = _to_float(row.get("acml_tr_pbmn") or row.get("frgn_ntby_tr_pbmn"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_us_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("symbol", "")
    name = row.get("longName", "") or row.get("shortName", symbol)
    price = _to_float(row.get("regularMarketPrice"))
    prev_close = _to_float(row.get("previousClose"))

    if price is not None and prev_close is not None and prev_close > 0:
        change_rate = ((price - prev_close) / prev_close) * 100
    else:
        change_rate = _to_float(row.get("regularMarketChangePercent", 0))

    volume = _to_int(row.get("regularMarketVolume"))
    market_cap = _to_float(row.get("marketCap"))
    trade_amount = None

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


def _map_crypto_row(row: dict, rank: int) -> dict[str, Any]:
    symbol = row.get("market", "")
    name = symbol.replace("KRW-", "") if symbol.startswith("KRW-") else symbol
    price = _to_float(row.get("trade_price"))
    change_rate = _normalize_change_rate_crypto(row.get("signed_change_rate"))
    volume = _to_float(row.get("acc_trade_volume_24h"))
    market_cap = None
    trade_amount = _to_float(row.get("acc_trade_price_24h"))

    return {
        "rank": rank,
        "symbol": symbol,
        "name": name,
        "price": price,
        "change_rate": round(change_rate, 2) if change_rate is not None else None,
        "volume": volume,
        "market_cap": market_cap,
        "trade_amount": trade_amount,
    }


# ---------------------------------------------------------------------------
# Ranking Fetchers
# ---------------------------------------------------------------------------


async def _get_us_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    return await _get_us_rankings_impl(ranking_type, limit, _map_us_row)


async def _get_crypto_rankings(
    ranking_type: str, limit: int
) -> tuple[list[dict[str, Any]], str]:
    return await _get_crypto_rankings_impl(ranking_type, limit, _map_crypto_row)


def _calculate_pearson_correlation(x: list[float], y: list[float]) -> float:
    return _calculate_pearson_correlation_impl(x, y)


# ---------------------------------------------------------------------------
# Analyze Stock Helpers
# ---------------------------------------------------------------------------


async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
    """Fetch quote data for any market type."""
    if market_type == "crypto":
        return await _fetch_quote_crypto(symbol)
    if market_type == "equity_kr":
        return await _fetch_quote_equity_kr(symbol)
    if market_type == "equity_us":
        return await _fetch_quote_equity_us(symbol)
    return None


async def _get_indicators_impl(
    symbol: str,
    indicators: list[str],
    market: str | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import _get_indicators_impl as _impl

    return await _impl(symbol, indicators, market)


async def _get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals_handlers import (
        _get_support_resistance_impl as _impl,
    )

    return await _impl(symbol, market)


async def _analyze_stock_impl(
    symbol: str,
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    market_type, normalized_symbol = _resolve_market_type(symbol, market)
    source_map = {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}
    source = source_map[market_type]

    errors: list[str] = []
    analysis: dict[str, Any] = {
        "symbol": normalized_symbol,
        "market_type": market_type,
        "source": source,
    }

    tasks: list[asyncio.Task[Any]] = []

    quote_task = asyncio.create_task(_get_quote_impl(normalized_symbol, market_type))
    tasks.append(quote_task)

    indicators_task = asyncio.create_task(
        _get_indicators_impl(normalized_symbol, ["rsi", "macd", "bollinger", "sma"], None),
    )
    tasks.append(indicators_task)

    sr_task = asyncio.create_task(
        _get_support_resistance_impl(normalized_symbol, None),
    )
    tasks.append(sr_task)

    if market_type == "equity_kr":
        valuation_task = asyncio.create_task(
            _fetch_valuation_naver(normalized_symbol),
        )
        tasks.append(valuation_task)

        news_task = asyncio.create_task(
            _fetch_news_naver(normalized_symbol, 5),
        )
        tasks.append(news_task)

        opinions_task = asyncio.create_task(
            _fetch_investment_opinions_naver(normalized_symbol, 10),
        )
        tasks.append(opinions_task)

    elif market_type == "equity_us":
        valuation_task = asyncio.create_task(
            _fetch_valuation_yfinance(normalized_symbol),
        )
        tasks.append(valuation_task)

        profile_task = asyncio.create_task(
            _fetch_company_profile_finnhub(normalized_symbol),
        )
        tasks.append(profile_task)

        news_task = asyncio.create_task(
            _fetch_news_finnhub(normalized_symbol, "us", 5),
        )
        tasks.append(news_task)

        opinions_task = asyncio.create_task(
            _fetch_investment_opinions_yfinance(normalized_symbol, 10),
        )
        tasks.append(opinions_task)

    elif market_type == "crypto":
        news_task = asyncio.create_task(
            _fetch_news_finnhub(normalized_symbol, "crypto", 5),
        )
        tasks.append(news_task)

    if include_peers and market_type != "crypto":
        if market_type == "equity_kr":
            peers_task = asyncio.create_task(
                _fetch_sector_peers_naver(normalized_symbol, 10),
            )
        else:
            peers_task = asyncio.create_task(
                _fetch_sector_peers_us(normalized_symbol, 10),
            )
        tasks.append(peers_task)

    results = await asyncio.gather(*tasks, return_exceptions=True)

    quote = None
    if not isinstance(results[0], Exception):
        quote = results[0]

    indicators = None
    if not isinstance(results[1], Exception) and len(results) > 1:
        indicators = results[1]

    support_resistance = None
    if not isinstance(results[2], Exception) and len(results) > 2:
        support_resistance = results[2]

    if quote:
        analysis["quote"] = quote

    if indicators:
        analysis["indicators"] = indicators

    if support_resistance:
        analysis["support_resistance"] = support_resistance

    task_idx = 3
    if market_type == "equity_kr":
        if not isinstance(results[task_idx], Exception):
            analysis["valuation"] = results[task_idx]
        task_idx += 1

        if not isinstance(results[task_idx], Exception):
            analysis["news"] = results[task_idx]
        task_idx += 1

        if not isinstance(results[task_idx], Exception):
            analysis["opinions"] = results[task_idx]
        task_idx += 1

    elif market_type == "equity_us":
        if not isinstance(results[task_idx], Exception):
            analysis["valuation"] = results[task_idx]
        task_idx += 1

        if not isinstance(results[task_idx], Exception):
            analysis["profile"] = results[task_idx]
        task_idx += 1

        if not isinstance(results[task_idx], Exception):
            analysis["news"] = results[task_idx]
        task_idx += 1

        if not isinstance(results[task_idx], Exception):
            analysis["opinions"] = results[task_idx]
        task_idx += 1

    elif market_type == "crypto":
        if not isinstance(results[task_idx], Exception):
            analysis["news"] = results[task_idx]

    if include_peers and market_type != "crypto":
        if not isinstance(results[task_idx], Exception):
            analysis["sector_peers"] = results[task_idx]

    if errors:
        analysis["errors"] = errors
    else:
        analysis["errors"] = []

    if market_type in ("equity_kr", "equity_us"):
        recommendation = _build_recommendation_for_equity(
            analysis, market_type
        )
        if recommendation:
            analysis["recommendation"] = recommendation

    return analysis


# ---------------------------------------------------------------------------
# Screen Stocks Helpers
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Recommend Stocks Helpers
# ---------------------------------------------------------------------------


async def _recommend_stocks_impl(
    *,
    budget: float,
    market: str,
    strategy: str,
    exclude_symbols: list[str] | None,
    sectors: list[str] | None,
    max_positions: int,
    top_stocks_fallback: Any,
) -> dict[str, Any]:
    return await _recommend_stocks_impl_core(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        top_stocks_fallback=top_stocks_fallback,
        screen_kr_fn=_screen_kr,
        screen_crypto_fn=_screen_crypto,
        top_stocks_override=top_stocks_fallback,
    )




# ---------------------------------------------------------------------------
# Public Helper Exports
# ---------------------------------------------------------------------------

__all__ = [
    "_error_payload",
    "_map_kr_row",
    "_map_us_row",
    "_map_crypto_row",
    "_get_us_rankings",
    "_get_crypto_rankings",
    "_calculate_pearson_correlation",
    "_get_quote_impl",
    "_analyze_stock_impl",
    "_recommend_stocks_impl",
]
