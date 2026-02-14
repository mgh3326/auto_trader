"""Analysis and screening MCP tool helper implementations."""

from __future__ import annotations

import asyncio
import re
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
    _allocate_budget,
    _build_recommend_reason,
    _normalize_candidate,
    _normalize_recommend_market,
)
from app.mcp_server.tooling.analysis_recommend import (
    recommend_stocks_impl as _recommend_stocks_impl_core,
)
from app.mcp_server.tooling.analysis_screen_core import (
    _apply_basic_filters,
    _build_screen_response,
    _normalize_asset_type,
    _normalize_dividend_yield_threshold,
    _normalize_screen_market,
    _normalize_sort_by,
    _normalize_sort_order,
    _screen_crypto,
    _screen_kr,
    _screen_us,
    _sort_and_limit,
    _validate_screen_filters,
)
from app.mcp_server.tooling.fundamentals_sources import (
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
from app.mcp_server.tooling.market_data import (
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
    _to_optional_int,
)
from app.mcp_server.tooling.shared import (
    _error_payload as _error_payload_impl,
)


def _error_payload(
    source: str, message: str, **kwargs: Any,
) -> dict[str, Any]:
    return _error_payload_impl(source=source, message=message, **kwargs)

# ---------------------------------------------------------------------------
# Naver Data Parsing Helpers
# ---------------------------------------------------------------------------


def _parse_naver_num(value: Any) -> float | None:
    """Parse a naver number which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (ValueError, TypeError):
        return None


def _parse_naver_int(value: Any) -> int | None:
    """Parse a naver integer which may be a string with commas."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(float(str(value).replace(",", "")))
    except (ValueError, TypeError):
        return None


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
# Crypto Symbol Normalization
# ---------------------------------------------------------------------------


def _normalize_crypto_base_symbol(symbol: str) -> str:
    """Normalize crypto symbol to base currency (e.g., 'KRW-BTC' -> 'BTC')."""
    normalized = symbol.upper().strip()
    if normalized.startswith("KRW-"):
        normalized = normalized[len("KRW-") :]
    if normalized.startswith("USDT-"):
        normalized = normalized[len("USDT-") :]
    if normalized.endswith("-KRW"):
        normalized = normalized[: -len("-KRW")]
    if normalized.endswith("-USDT"):
        normalized = normalized[: -len("-USDT")]
    if normalized.endswith("USDT"):
        normalized = normalized[: -len("USDT")]

    return normalized


# ---------------------------------------------------------------------------
# CoinGecko Helpers
# ---------------------------------------------------------------------------


def _coingecko_cache_valid(expires_at: Any, now: float) -> bool:
    try:
        return float(expires_at) > now
    except Exception:
        return False


def _to_optional_money(value: Any) -> int | None:
    numeric = _to_optional_float(value)
    if numeric is None:
        return None
    return int(round(numeric))


def _clean_description_one_line(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return None

    if len(text) > 240:
        text = text[:240].rstrip() + "..."
    return text


def _map_coingecko_profile_to_output(profile: dict[str, Any]) -> dict[str, Any]:
    market_data = profile.get("market_data") or {}
    description_map = profile.get("description") or {}

    description = _clean_description_one_line(
        description_map.get("ko") or description_map.get("en")
    )

    market_cap_krw = _to_optional_money(
        (market_data.get("market_cap") or {}).get("krw")
    )
    total_volume_krw = _to_optional_money(
        (market_data.get("total_volume") or {}).get("krw")
    )
    ath_krw = _to_optional_money((market_data.get("ath") or {}).get("krw"))

    ath_change_pct = _to_optional_float(
        (market_data.get("ath_change_percentage") or {}).get("krw")
    )
    change_7d = _to_optional_float(
        (market_data.get("price_change_percentage_7d_in_currency") or {}).get("krw")
    )
    if change_7d is None:
        change_7d = _to_optional_float(market_data.get("price_change_percentage_7d"))

    change_30d = _to_optional_float(
        (market_data.get("price_change_percentage_30d_in_currency") or {}).get("krw")
    )
    if change_30d is None:
        change_30d = _to_optional_float(market_data.get("price_change_percentage_30d"))

    categories = profile.get("categories")
    if not isinstance(categories, list):
        categories = []

    return {
        "name": profile.get("name"),
        "symbol": str(profile.get("symbol") or "").upper() or None,
        "market_cap": market_cap_krw,
        "market_cap_rank": _to_optional_int(profile.get("market_cap_rank")),
        "total_volume_24h": total_volume_krw,
        "circulating_supply": _to_optional_float(market_data.get("circulating_supply")),
        "total_supply": _to_optional_float(market_data.get("total_supply")),
        "max_supply": _to_optional_float(market_data.get("max_supply")),
        "categories": categories,
        "description": description,
        "ath": ath_krw,
        "ath_change_percentage": ath_change_pct,
        "price_change_percentage_7d": change_7d,
        "price_change_percentage_30d": change_30d,
    }


# ---------------------------------------------------------------------------
# Funding Rate Helpers
# ---------------------------------------------------------------------------


def _funding_interpretation_text(rate: float) -> str:
    if rate > 0:
        return "positive (롱이 숏에게 지불, 롱 과열)"
    if rate < 0:
        return "negative (숏이 롱에게 지불, 숏 과열)"
    return "neutral"


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
    from app.mcp_server.tooling.portfolio import _get_indicators_impl as _impl

    return await _impl(symbol, indicators, market)


async def _get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals import (
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
# Backward Compatibility Aliases
# ---------------------------------------------------------------------------

__all__ = [
    "_parse_naver_num",
    "_parse_naver_int",
    "_parse_change_rate",
    "_normalize_change_rate_equity",
    "_normalize_change_rate_crypto",
    "_map_kr_row",
    "_map_us_row",
    "_map_crypto_row",
    "_normalize_crypto_base_symbol",
    "_coingecko_cache_valid",
    "_to_optional_money",
    "_clean_description_one_line",
    "_map_coingecko_profile_to_output",
    "_funding_interpretation_text",
    "_get_us_rankings",
    "_get_crypto_rankings",
    "_calculate_pearson_correlation",
    "_get_quote_impl",
    "_analyze_stock_impl",
    "_normalize_screen_market",
    "_normalize_asset_type",
    "_normalize_sort_by",
    "_normalize_sort_order",
    "_normalize_dividend_yield_threshold",
    "_validate_screen_filters",
    "_apply_basic_filters",
    "_sort_and_limit",
    "_build_screen_response",
    "_screen_kr",
    "_screen_us",
    "_screen_crypto",
    "_normalize_recommend_market",
    "_build_recommend_reason",
    "_normalize_candidate",
    "_allocate_budget",
    "_recommend_stocks_impl",
]
