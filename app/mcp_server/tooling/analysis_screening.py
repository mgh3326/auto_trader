"""Analysis and screening MCP tool helper implementations."""

from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
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
    recommend_stocks_impl as _recommend_stocks_impl_core,
)
from app.mcp_server.tooling.analysis_screen_core import (
    _screen_crypto,
    _screen_kr,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
    _fetch_news_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_analysis_snapshot_naver,
    _fetch_investment_opinions_yfinance,
    _fetch_sector_peers_naver,
    _fetch_sector_peers_us,
    _fetch_valuation_yfinance,
    _YFinanceSnapshot,
)
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
)
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import (
    build_recommendation_for_equity as _build_recommendation_for_equity,
)
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload_impl,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.mcp_server.tooling.shared import (
    resolve_market_type as _resolve_market_type,
)
from app.mcp_server.tooling.shared import (
    to_float as _to_float,
)
from app.mcp_server.tooling.shared import (
    to_int as _to_int,
)
from app.mcp_server.tooling.shared import (
    to_optional_float as _to_optional_float,
)
from app.monitoring import build_yfinance_tracing_session


def _error_payload(
    source: str,
    message: str,
    **kwargs: Any,
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


def _map_kr_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
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


def _map_us_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
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


def _map_crypto_row(row: dict[str, Any], rank: int) -> dict[str, Any]:
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


def _build_kr_quote_from_ohlcv(
    symbol: str, ohlcv_df: pd.DataFrame
) -> dict[str, Any] | None:
    if ohlcv_df.empty:
        return None
    last = ohlcv_df.iloc[-1].to_dict()
    return {
        "symbol": symbol,
        "instrument_type": "equity_kr",
        "price": last.get("close"),
        "open": last.get("open"),
        "high": last.get("high"),
        "low": last.get("low"),
        "volume": last.get("volume"),
        "value": last.get("value"),
        "source": "kis",
    }


async def _get_indicators_impl(
    symbol: str,
    indicators: list[str],
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.portfolio_holdings import _get_indicators_impl as _impl

    return await _impl(symbol, indicators, market, preloaded_df=preloaded_df)


async def _get_support_resistance_impl(
    symbol: str,
    market: str | None = None,
    preloaded_df: pd.DataFrame | None = None,
) -> dict[str, Any]:
    from app.mcp_server.tooling.fundamentals_handlers import (
        _get_support_resistance_impl as _impl,
    )

    return await _impl(symbol, market, preloaded_df=preloaded_df)


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

    named_tasks: list[tuple[str, asyncio.Task[Any]]] = []

    loop = asyncio.get_running_loop()
    ohlcv_df = await _fetch_ohlcv_for_indicators(
        normalized_symbol, market_type, count=250
    )
    ohlcv_60d = ohlcv_df.tail(60) if len(ohlcv_df) >= 60 else ohlcv_df

    preloaded_quote = None
    if market_type == "equity_kr":
        preloaded_quote = _build_kr_quote_from_ohlcv(normalized_symbol, ohlcv_df)
        if preloaded_quote is None:
            named_tasks.append(
                (
                    "quote",
                    asyncio.create_task(
                        _get_quote_impl(normalized_symbol, market_type)
                    ),
                )
            )
    else:
        named_tasks.append(
            (
                "quote",
                asyncio.create_task(_get_quote_impl(normalized_symbol, market_type)),
            )
        )

    named_tasks.append(
        (
            "indicators",
            asyncio.create_task(
                _get_indicators_impl(
                    normalized_symbol,
                    ["rsi", "macd", "bollinger", "sma"],
                    None,
                    preloaded_df=ohlcv_df,
                )
            ),
        )
    )
    named_tasks.append(
        (
            "support_resistance",
            asyncio.create_task(
                _get_support_resistance_impl(
                    normalized_symbol, None, preloaded_df=ohlcv_60d
                )
            ),
        )
    )

    if market_type == "equity_kr":
        named_tasks.append(
            (
                "kr_snapshot",
                asyncio.create_task(
                    _fetch_analysis_snapshot_naver(normalized_symbol, 5, 10)
                ),
            )
        )

    elif market_type == "equity_us":
        # Collect yfinance snapshot once to avoid duplicate ticker.info calls
        yf_session = build_yfinance_tracing_session()
        yf_ticker = yf.Ticker(normalized_symbol, session=yf_session)

        def _collect_yf_snapshot() -> _YFinanceSnapshot:
            info = None
            targets = None
            ud = None
            try:
                info = yf_ticker.info
            except Exception:
                pass
            try:
                targets = yf_ticker.analyst_price_targets
            except Exception:
                pass
            try:
                ud = yf_ticker.upgrades_downgrades
            except Exception:
                pass
            return _YFinanceSnapshot(
                info=info, analyst_price_targets=targets, upgrades_downgrades=ud
            )

        yf_snapshot = await loop.run_in_executor(None, _collect_yf_snapshot)

        named_tasks.append(
            (
                "valuation",
                asyncio.create_task(
                    _fetch_valuation_yfinance(
                        normalized_symbol, snapshot=yf_snapshot, session=yf_session
                    )
                ),
            )
        )
        named_tasks.append(
            (
                "profile",
                asyncio.create_task(_fetch_company_profile_finnhub(normalized_symbol)),
            )
        )
        named_tasks.append(
            (
                "news",
                asyncio.create_task(_fetch_news_finnhub(normalized_symbol, "us", 5)),
            )
        )
        named_tasks.append(
            (
                "opinions",
                asyncio.create_task(
                    _fetch_investment_opinions_yfinance(
                        normalized_symbol, 10, snapshot=yf_snapshot, session=yf_session
                    )
                ),
            )
        )

    elif market_type == "crypto":
        named_tasks.append(
            (
                "news",
                asyncio.create_task(
                    _fetch_news_finnhub(normalized_symbol, "crypto", 5)
                ),
            )
        )

    if include_peers and market_type != "crypto":
        if market_type == "equity_kr":
            peers_task = asyncio.create_task(
                _fetch_sector_peers_naver(normalized_symbol, 10)
            )
        else:
            peers_task = asyncio.create_task(
                _fetch_sector_peers_us(normalized_symbol, 10)
            )
        named_tasks.append(("sector_peers", peers_task))

    results = await asyncio.gather(
        *(task for _, task in named_tasks), return_exceptions=True
    )
    task_results = {
        name: result
        for (name, _), result in zip(named_tasks, results, strict=True)
        if not isinstance(result, Exception)
    }

    quote = preloaded_quote or task_results.get("quote")
    indicators = task_results.get("indicators")
    support_resistance = task_results.get("support_resistance")

    if quote:
        analysis["quote"] = quote

    if indicators:
        analysis["indicators"] = indicators

    if support_resistance:
        analysis["support_resistance"] = support_resistance

    if market_type == "equity_kr":
        kr_snapshot = task_results.get("kr_snapshot")
        if isinstance(kr_snapshot, dict):
            if "valuation" in kr_snapshot:
                analysis["valuation"] = kr_snapshot["valuation"]
            if "news" in kr_snapshot:
                analysis["news"] = kr_snapshot["news"]
            if "opinions" in kr_snapshot:
                analysis["opinions"] = kr_snapshot["opinions"]

    elif market_type == "equity_us":
        if "valuation" in task_results:
            analysis["valuation"] = task_results["valuation"]
        if "profile" in task_results:
            analysis["profile"] = task_results["profile"]
        if "news" in task_results:
            analysis["news"] = task_results["news"]
        if "opinions" in task_results:
            analysis["opinions"] = task_results["opinions"]

    elif market_type == "crypto":
        if "news" in task_results:
            analysis["news"] = task_results["news"]

    if include_peers and market_type != "crypto":
        if "sector_peers" in task_results:
            analysis["sector_peers"] = task_results["sector_peers"]

    if errors:
        analysis["errors"] = errors
    else:
        analysis["errors"] = []

    if market_type in ("equity_kr", "equity_us"):
        recommendation = _build_recommendation_for_equity(analysis, market_type)
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
    exclude_held: bool = True,
    top_stocks_fallback: Any,
) -> dict[str, Any]:
    return await _recommend_stocks_impl_core(
        budget=budget,
        market=market,
        strategy=strategy,
        exclude_symbols=exclude_symbols,
        sectors=sectors,
        max_positions=max_positions,
        exclude_held=exclude_held,
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
