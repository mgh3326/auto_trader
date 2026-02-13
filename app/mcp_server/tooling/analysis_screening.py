"""Analysis and screening MCP tool helpers and registrations."""

from __future__ import annotations

import asyncio
import datetime
import re
from typing import TYPE_CHECKING, Any, Literal

import httpx
import yfinance as yf

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
    _fetch_ohlcv_for_indicators,
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import (
    _build_recommendation_for_equity,
    _error_payload,
    _normalize_symbol_input,
    _resolve_market_type,
    _to_float,
    _to_int,
    _to_optional_float,
    _to_optional_int,
)
from app.services.kis import KISClient

try:
    from app.services.disclosures.dart import list_filings
except ImportError:
    list_filings = None

if TYPE_CHECKING:
    from fastmcp import FastMCP

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


def _resolve_analyze_stock_impl() -> Any:
    """Prefer migrated implementation, but respect test monkeypatches."""
    from app.mcp_server import tools as mcp_tools

    external_impl = getattr(mcp_tools, "_analyze_stock_impl", None)
    if callable(external_impl):
        module_name = getattr(external_impl, "__module__", "")
        if module_name != "app.mcp_server.tooling.legacy_tools":
            return external_impl
    return _analyze_stock_impl


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
        top_stocks_override=globals().get("get_top_stocks"),
    )


# ---------------------------------------------------------------------------
# Tool Registration
# ---------------------------------------------------------------------------

ANALYSIS_TOOL_NAMES: set[str] = {
    "analyze_stock",
    "analyze_portfolio",
    "screen_stocks",
    "recommend_stocks",
    "get_top_stocks",
    "get_disclosures",
    "get_correlation",
    "get_dividends",
    "get_fear_greed_index",
}


def register_analysis_tools(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_top_stocks",
        description=(
            "Get top stocks by ranking type across different markets (KR/US/Crypto). "
            "KR: volume, market_cap, gainers, losers, foreigners "
            "US: volume, market_cap, gainers, losers "
            "Crypto: volume, gainers, losers."
        ),
    )
    async def get_top_stocks(
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
        source = {
            "kr": "kis",
            "us": "yfinance",
            "crypto": "upbit",
        }.get(market, "")

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
                rankings, source = await _get_crypto_rankings(
                    ranking_type, limit_clamped
                )

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

    @mcp.tool(
        name="get_disclosures",
        description=(
            "Get DART (OPENDART) disclosure filings for Korean corporations. "
            "Supports both 6-digit corp codes (e.g., '005930') and Korean company names "
            "(e.g., '삼성전자'). Returns filing date, report name, report number, and "
            "corporation name."
        ),
    )
    async def get_disclosures(
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
        try:
            result = await list_filings(symbol, days, limit, report_type)
            if isinstance(result, list):
                return {"success": True, "filings": result}
            return result
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "symbol": symbol,
            }

    @mcp.tool(
        name="get_correlation",
        description=(
            "Calculate Pearson correlation matrix between multiple assets. "
            "Supports Korean stocks (KIS), US stocks (yfinance), and crypto (Upbit). "
            "Uses daily closing prices over specified period."
        ),
    )
    async def get_correlation(
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
                market_type, normalized_symbol = _resolve_market_type(
                    symbol, None
                )
                market_types[normalized_symbol] = market_type

                df = await _fetch_ohlcv_for_indicators(
                    normalized_symbol, market_type, count=period
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
                "error": "Insufficient data to calculate correlation (need at least 2 symbols)",
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

    @mcp.tool(
        name="analyze_stock",
        description=(
            "Comprehensive stock analysis tool. Fetches quote, indicators (RSI, MACD, "
            "BB, SMA), support/resistance, and market-specific data in parallel. "
            "For Korean stocks: valuation (Naver), news (Naver), opinions (Naver). "
            "For US stocks: valuation (yfinance), profile (Finnhub), news (Finnhub), "
            "opinions (yfinance). For crypto: news (Finnhub). "
            "Optionally includes sector peers. Returns errors array for failed "
            "sections."
        ),
    )
    async def analyze_stock(
        symbol: str | int,
        market: str | None = None,
        include_peers: bool = False,
    ) -> dict[str, Any]:
        normalized_symbol = _normalize_symbol_input(symbol, market)
        impl = _resolve_analyze_stock_impl()
        result = impl(normalized_symbol, market, include_peers)
        if asyncio.iscoroutine(result):
            return await result
        return result

    @mcp.tool(
        name="analyze_portfolio",
        description=(
            "Analyze multiple stocks in parallel. Returns individual analysis for "
            "each symbol plus portfolio summary. Maximum 5 concurrent analyses."
        ),
    )
    async def analyze_portfolio(
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
        impl = _resolve_analyze_stock_impl()

        async def _analyze_one(sym: str) -> dict[str, Any]:
            async with sem:
                try:
                    result = impl(sym, market, include_peers)
                    if asyncio.iscoroutine(result):
                        return await result
                    return result
                except Exception as exc:
                    errors.append(f"{sym}: {str(exc)}")
                    return {"symbol": sym, "error": str(exc)}

        analyze_results = await asyncio.gather(*[_analyze_one(s) for s in normalized_symbols])

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

    @mcp.tool(
        name="screen_stocks",
        description=(
            "Screen stocks across different markets (KR/US/Crypto) with various filters. "
            "KR market uses KRX for stocks/ETFs + valuation metrics. "
            "Supports KOSPI/KOSDAQ sub-market filtering (market='kospi' or 'kosdaq'). "
            "US market uses yfinance screener. "
            "Crypto market uses Upbit top traded coins."
        ),
    )
    async def screen_stocks(
        market: Literal["kr", "kospi", "kosdaq", "us", "crypto"] = "kr",
        asset_type: Literal["stock", "etf", "etn"] | None = None,
        category: str | None = None,
        sort_by: Literal["volume", "market_cap", "change_rate", "dividend_yield"] = "volume",
        sort_order: Literal["asc", "desc"] = "desc",
        min_market_cap: float | None = None,
        max_per: float | None = None,
        max_pbr: float | None = None,
        min_dividend_yield: float | None = None,
        max_rsi: float | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        market = _normalize_screen_market(market)
        asset_type = _normalize_asset_type(asset_type)
        sort_by = _normalize_sort_by(sort_by)
        sort_order = _normalize_sort_order(sort_order)

        if limit < 1:
            raise ValueError("limit must be at least 1")
        if limit > 50:
            limit = 50

        _validate_screen_filters(
            market=market,
            asset_type=asset_type,
            min_market_cap=min_market_cap,
            max_per=max_per,
            min_dividend_yield=min_dividend_yield,
            max_rsi=max_rsi,
            sort_by=sort_by,
        )

        if market in ("kr", "kospi", "kosdaq"):
            return await _screen_kr(
                market=market,
                asset_type=asset_type,
                category=category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                max_pbr=max_pbr,
                min_dividend_yield=min_dividend_yield,
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
            )
        if market == "us":
            return await _screen_us(
                market=market,
                asset_type=asset_type,
                category=category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                min_dividend_yield=min_dividend_yield,
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
            )
        if market == "crypto":
            return await _screen_crypto(
                market=market,
                asset_type=asset_type,
                category=category,
                min_market_cap=min_market_cap,
                max_per=max_per,
                min_dividend_yield=min_dividend_yield,
                max_rsi=max_rsi,
                sort_by=sort_by,
                sort_order=sort_order,
                limit=limit,
            )

        return _error_payload(
            source="screen_stocks",
            message=f"Unsupported market: {market}",
        )

    @mcp.tool(
        name="recommend_stocks",
        description=(
            "예산과 전략에 따라 주식을 추천합니다. "
            "screen_stocks를 활용해 후보를 추출하고 전략별 가중치 점수로 "
            "보유 종목을 제외한 뒤 예산을 배분합니다. "
            "지원 전략: balanced, growth, value, dividend, momentum"
        ),
    )
    async def recommend_stocks(
        budget: float,
        market: str = "kr",
        strategy: str = "balanced",
        exclude_symbols: list[str] | None = None,
        sectors: list[str] | None = None,
        max_positions: int = 5,
    ) -> dict[str, Any]:
        return await _recommend_stocks_impl(
            budget=budget,
            market=market,
            strategy=strategy,
            exclude_symbols=exclude_symbols,
            sectors=sectors,
            max_positions=max_positions,
            top_stocks_fallback=get_top_stocks,
        )

    @mcp.tool(
        name="get_dividends",
        description=(
            "Get dividend information for US stocks (via yfinance). Returns dividend "
            "yield, payout date, and latest dividend info."
        ),
    )
    async def get_dividends(symbol: str) -> dict[str, Any]:
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
                    "dividend_yield": round(dividend_yield, 4)
                    if dividend_yield
                    else None,
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

    @mcp.tool(
        name="get_fear_greed_index",
        description=(
            "Get the Crypto Fear & Greed Index from Alternative.me. Returns current "
            "value/classification and requested historical values."
        ),
    )
    async def get_fear_greed_index(days: int = 7) -> dict[str, Any]:
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
                    {
                        "date": date,
                        "value": value,
                        "classification": classification,
                    }
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

    # Export nested callable for backward-compatible monkeypatching.
    globals()["get_top_stocks"] = get_top_stocks


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
    "_resolve_analyze_stock_impl",
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
    "ANALYSIS_TOOL_NAMES",
    "register_analysis_tools",
]
