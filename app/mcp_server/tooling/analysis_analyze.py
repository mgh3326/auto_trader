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


async def _get_quote_impl(symbol: str, market_type: str) -> dict[str, Any] | None:
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

    tasks: list[asyncio.Task[Any]] = []

    ohlcv_df = await _fetch_ohlcv_for_indicators(
        normalized_symbol, market_type, count=250
    )
    ohlcv_60d = ohlcv_df.tail(60) if len(ohlcv_df) >= 60 else ohlcv_df

    loop = asyncio.get_running_loop()

    quote_task = asyncio.create_task(_get_quote_impl(normalized_symbol, market_type))
    tasks.append(quote_task)

    indicators_task = asyncio.create_task(
        _get_indicators_impl(
            normalized_symbol,
            ["rsi", "macd", "bollinger", "sma"],
            None,
            preloaded_df=ohlcv_df,
        ),
    )
    tasks.append(indicators_task)

    sr_task = asyncio.create_task(
        _get_support_resistance_impl(normalized_symbol, None, preloaded_df=ohlcv_60d),
    )
    tasks.append(sr_task)

    if market_type == "equity_kr":
        valuation_task = asyncio.create_task(_fetch_valuation_naver(normalized_symbol))
        tasks.append(valuation_task)

        news_task = asyncio.create_task(_fetch_news_naver(normalized_symbol, 5))
        tasks.append(news_task)

        opinions_task = asyncio.create_task(
            _fetch_investment_opinions_naver(normalized_symbol, 10),
        )
        tasks.append(opinions_task)

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

        valuation_task = asyncio.create_task(
            _fetch_valuation_yfinance(
                normalized_symbol, snapshot=yf_snapshot, session=yf_session
            ),
        )
        tasks.append(valuation_task)

        profile_task = asyncio.create_task(
            _fetch_company_profile_finnhub(normalized_symbol),
        )
        tasks.append(profile_task)

        news_task = asyncio.create_task(_fetch_news_finnhub(normalized_symbol, "us", 5))
        tasks.append(news_task)

        opinions_task = asyncio.create_task(
            _fetch_investment_opinions_yfinance(
                normalized_symbol, 10, snapshot=yf_snapshot, session=yf_session
            ),
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
        recommendation = _build_recommendation_for_equity(analysis, market_type)
        if recommendation:
            analysis["recommendation"] = recommendation

    return analysis


__all__ = ["analyze_stock_impl"]
