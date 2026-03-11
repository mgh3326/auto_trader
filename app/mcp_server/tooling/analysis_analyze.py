from __future__ import annotations

import asyncio
from typing import Any

import pandas as pd
import yfinance as yf

from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
    _fetch_news_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_analysis_snapshot_naver,
    _fetch_investment_opinions_naver,
    _fetch_investment_opinions_yfinance,
    _fetch_news_naver,
    _fetch_sector_peers_naver,
    _fetch_sector_peers_us,
    _fetch_valuation_naver,
    _fetch_valuation_yfinance,
    _YFinanceSnapshot,
)
from app.mcp_server.tooling.market_data_indicators import _fetch_ohlcv_for_indicators
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_quote_crypto,
    _fetch_quote_equity_kr,
    _fetch_quote_equity_us,
)
from app.mcp_server.tooling.shared import (
    build_recommendation_for_equity as _build_recommendation_for_equity,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.mcp_server.tooling.shared import resolve_market_type as _resolve_market_type
from app.monitoring import build_yfinance_tracing_session

# Keep direct KR helper bindings available for test monkeypatch compatibility.
_KR_ANALYZE_PATCH_SURFACES = (
    _fetch_investment_opinions_naver,
    _fetch_news_naver,
    _fetch_valuation_naver,
)

DEFAULT_ANALYZE_STOCK_INDICATORS: tuple[str, ...] = (
    "rsi",
    "macd",
    "bollinger",
    "sma",
    "adx",
    "stoch_rsi",
)


async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
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


async def analyze_stock_impl(
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
                    list(DEFAULT_ANALYZE_STOCK_INDICATORS),
                    None,
                    preloaded_df=ohlcv_df,
                )
            ),
        ),
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
                info=info,
                analyst_price_targets=targets,
                upgrades_downgrades=ud,
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
                _fetch_sector_peers_naver(normalized_symbol, 10),
            )
        else:
            peers_task = asyncio.create_task(
                _fetch_sector_peers_us(normalized_symbol, 10),
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


__all__ = ["analyze_stock_impl"]
