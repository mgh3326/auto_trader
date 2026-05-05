from __future__ import annotations

import asyncio
import logging
from typing import Any

import pandas as pd
import sentry_sdk
import yfinance as yf
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.models.research_pipeline import ResearchSession, ResearchSummary, StageAnalysis
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
    _fetch_news_finnhub,
)
from app.mcp_server.tooling.fundamentals_sources_naver import (
    _fetch_analysis_snapshot_naver,
    _fetch_investment_opinions_naver,
    _fetch_news_naver,
    _fetch_sector_peers_naver,
    _fetch_valuation_naver,
)
from app.mcp_server.tooling.fundamentals_sources_yfinance import (
    _fetch_investment_opinions_yfinance,
    _fetch_sector_peers_us,
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

logger = logging.getLogger(__name__)

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


def _analysis_source(market_type: str) -> str:
    return {"crypto": "upbit", "equity_kr": "kis", "equity_us": "yahoo"}[market_type]


def _build_analysis_payload(
    normalized_symbol: str,
    market_type: str,
) -> dict[str, Any]:
    return {
        "symbol": normalized_symbol,
        "market_type": market_type,
        "source": _analysis_source(market_type),
    }


def _prepare_quote_tasks(
    normalized_symbol: str,
    market_type: str,
    ohlcv_df: pd.DataFrame,
) -> tuple[dict[str, Any] | None, list[tuple[str, asyncio.Task[Any]]]]:
    preloaded_quote = None
    named_tasks: list[tuple[str, asyncio.Task[Any]]] = []

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
        return preloaded_quote, named_tasks

    named_tasks.append(
        (
            "quote",
            asyncio.create_task(_get_quote_impl(normalized_symbol, market_type)),
        )
    )
    return preloaded_quote, named_tasks


def _append_common_tasks(
    named_tasks: list[tuple[str, asyncio.Task[Any]]],
    normalized_symbol: str,
    ohlcv_df: pd.DataFrame,
    ohlcv_60d: pd.DataFrame,
) -> None:
    named_tasks.extend(
        [
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
            (
                "support_resistance",
                asyncio.create_task(
                    _get_support_resistance_impl(
                        normalized_symbol,
                        None,
                        preloaded_df=ohlcv_60d,
                    )
                ),
            ),
        ]
    )


def _collect_yfinance_snapshot(yf_ticker: Any) -> _YFinanceSnapshot:
    info = None
    targets = None
    recommendations = None
    upgrades_downgrades = None
    try:
        info = yf_ticker.info
    except Exception:
        pass
    try:
        targets = yf_ticker.analyst_price_targets
    except Exception:
        pass
    try:
        recommendations = yf_ticker.recommendations
    except Exception:
        pass
    try:
        upgrades_downgrades = yf_ticker.upgrades_downgrades
    except Exception:
        pass
    return _YFinanceSnapshot(
        info=info,
        analyst_price_targets=targets,
        recommendations=recommendations,
        upgrades_downgrades=upgrades_downgrades,
    )


async def _append_market_specific_tasks(
    named_tasks: list[tuple[str, asyncio.Task[Any]]],
    normalized_symbol: str,
    market_type: str,
    loop: asyncio.AbstractEventLoop,
) -> None:
    if market_type == "equity_kr":
        named_tasks.append(
            (
                "kr_snapshot",
                asyncio.create_task(
                    _fetch_analysis_snapshot_naver(normalized_symbol, 5, 10)
                ),
            )
        )
        return

    if market_type == "crypto":
        named_tasks.append(
            (
                "news",
                asyncio.create_task(
                    _fetch_news_finnhub(normalized_symbol, "crypto", 5)
                ),
            )
        )
        return

    yf_session = build_yfinance_tracing_session()
    yf_ticker = yf.Ticker(normalized_symbol, session=yf_session)
    yf_snapshot = await loop.run_in_executor(
        None, _collect_yfinance_snapshot, yf_ticker
    )
    named_tasks.extend(
        [
            (
                "valuation",
                asyncio.create_task(
                    _fetch_valuation_yfinance(
                        normalized_symbol,
                        snapshot=yf_snapshot,
                        session=yf_session,
                    )
                ),
            ),
            (
                "profile",
                asyncio.create_task(_fetch_company_profile_finnhub(normalized_symbol)),
            ),
            (
                "news",
                asyncio.create_task(_fetch_news_finnhub(normalized_symbol, "us", 5)),
            ),
            (
                "opinions",
                asyncio.create_task(
                    _fetch_investment_opinions_yfinance(
                        normalized_symbol,
                        10,
                        snapshot=yf_snapshot,
                        session=yf_session,
                    )
                ),
            ),
        ]
    )


def _append_sector_peers_task(
    named_tasks: list[tuple[str, asyncio.Task[Any]]],
    normalized_symbol: str,
    market_type: str,
    include_peers: bool,
) -> None:
    if not include_peers or market_type == "crypto":
        return
    if market_type == "equity_kr":
        peers_task = asyncio.create_task(
            _fetch_sector_peers_naver(normalized_symbol, 10)
        )
    else:
        peers_task = asyncio.create_task(_fetch_sector_peers_us(normalized_symbol, 10))
    named_tasks.append(("sector_peers", peers_task))


async def _gather_task_results(
    named_tasks: list[tuple[str, asyncio.Task[Any]]],
) -> dict[str, Any]:
    results = await asyncio.gather(
        *(task for _, task in named_tasks),
        return_exceptions=True,
    )
    return {
        name: result
        for (name, _), result in zip(named_tasks, results, strict=True)
        if not isinstance(result, Exception)
    }


def _apply_common_results(
    analysis: dict[str, Any],
    task_results: dict[str, Any],
    preloaded_quote: dict[str, Any] | None,
) -> None:
    quote = preloaded_quote or task_results.get("quote")
    if quote:
        analysis["quote"] = quote
    for key in ("indicators", "support_resistance"):
        value = task_results.get(key)
        if not value:
            continue
        # Normalize indicators payload: unwrap provider-style payload
        # Provider returns: {"symbol": ..., "price": ..., "indicators": {...}}
        # We want: {"rsi": {...}, ...} (flat indicator map)
        if key == "indicators" and isinstance(value, dict):
            # Skip error payloads
            if "error" in value:
                continue
            inner = value.get("indicators")
            if isinstance(inner, dict):
                value = inner
        analysis[key] = value


def _apply_kr_results(analysis: dict[str, Any], task_results: dict[str, Any]) -> None:
    kr_snapshot = task_results.get("kr_snapshot")
    if not isinstance(kr_snapshot, dict):
        return
    for key in ("valuation", "news", "opinions"):
        if key in kr_snapshot:
            analysis[key] = kr_snapshot[key]


def _apply_us_results(analysis: dict[str, Any], task_results: dict[str, Any]) -> None:
    for key in ("valuation", "profile", "news", "opinions"):
        if key in task_results:
            analysis[key] = task_results[key]


def _apply_market_specific_results(
    analysis: dict[str, Any],
    task_results: dict[str, Any],
    market_type: str,
) -> None:
    if market_type == "equity_kr":
        _apply_kr_results(analysis, task_results)
        return
    if market_type == "equity_us":
        _apply_us_results(analysis, task_results)
        return
    if "news" in task_results:
        analysis["news"] = task_results["news"]


def _apply_sector_peers_result(
    analysis: dict[str, Any],
    task_results: dict[str, Any],
    market_type: str,
    include_peers: bool,
) -> None:
    if include_peers and market_type != "crypto" and "sector_peers" in task_results:
        analysis["sector_peers"] = task_results["sector_peers"]


def _apply_recommendation(
    analysis: dict[str, Any],
    market_type: str,
) -> None:
    if market_type not in ("equity_kr", "equity_us"):
        return
    recommendation = _build_recommendation_for_equity(analysis, market_type)
    if recommendation:
        analysis["recommendation"] = recommendation


def _map_confidence_score(score: int) -> str:
    if score >= 70:
        return "high"
    if score >= 40:
        return "medium"
    return "low"


def _map_pipeline_to_analysis(
    session: ResearchSession,
    summary: ResearchSummary,
    symbol: str,
    market_type: str,
) -> dict[str, Any]:
    # Extract price analysis fields
    pa = summary.price_analysis or {}

    buy_zones = []
    if pa.get("appropriate_buy_min") is not None or pa.get("appropriate_buy_max") is not None:
        buy_zones.append(
            {
                "price": pa.get("appropriate_buy_max") or pa.get("appropriate_buy_min"),
                "type": "appropriate_buy",
                "reasoning": f"Appropriate buy range: {pa.get('appropriate_buy_min')} - {pa.get('appropriate_buy_max')}",
            }
        )
    if pa.get("buy_hope_min") is not None or pa.get("buy_hope_max") is not None:
        buy_zones.append(
            {
                "price": pa.get("buy_hope_max") or pa.get("buy_hope_min"),
                "type": "buy_hope",
                "reasoning": f"Buy hope range: {pa.get('buy_hope_min')} - {pa.get('buy_hope_max')}",
            }
        )

    sell_targets = []
    if (
        pa.get("appropriate_sell_min") is not None
        or pa.get("appropriate_sell_max") is not None
    ):
        sell_targets.append(
            {
                "price": pa.get("appropriate_sell_min") or pa.get("appropriate_sell_max"),
                "type": "appropriate_sell",
                "reasoning": f"Appropriate sell range: {pa.get('appropriate_sell_min')} - {pa.get('appropriate_sell_max')}",
            }
        )
    if pa.get("sell_target_min") is not None or pa.get("sell_target_max") is not None:
        sell_targets.append(
            {
                "price": pa.get("sell_target_min") or pa.get("sell_target_max"),
                "type": "sell_target",
                "reasoning": f"Sell target range: {pa.get('sell_target_min')} - {pa.get('sell_target_max')}",
            }
        )

    # Sort and limit
    buy_zones.sort(key=lambda x: x["price"] if x["price"] is not None else 0)
    sell_targets.sort(key=lambda x: x["price"] if x["price"] is not None else 0)

    # Recommendation
    recommendation = {
        "action": (
            summary.decision.value
            if hasattr(summary.decision, "value")
            else str(summary.decision)
        ),
        "confidence": _map_confidence_score(summary.confidence),
        "buy_zones": buy_zones[:3],
        "sell_targets": sell_targets[:3],
        "stop_loss": None,  # PriceAnalysis doesn't have it explicitly yet
        "reasoning": "; ".join(summary.reasons) if summary.reasons else "",
        "detailed_text": summary.detailed_text,
        "bull_arguments": summary.bull_arguments,
        "bear_arguments": summary.bear_arguments,
    }

    # Map stages back to indicators/news/etc if possible
    analysis = {
        "symbol": symbol,
        "market_type": market_type,
        "source": "research_pipeline",
        "session_id": session.id,
        "recommendation": recommendation,
        "errors": summary.warnings or [],
        "pipeline_metadata": {
            "model_name": summary.model_name,
            "prompt_version": summary.prompt_version,
            "executed_at": (
                summary.executed_at.isoformat() if summary.executed_at else None
            ),
        },
    }

    # Try to extract some quote/indicators from stages if available
    for stage in session.stage_analyses:
        if stage.stage_type == "market":
            analysis["quote"] = {
                "price": stage.signals.get("last_close"),
                "change_pct": stage.signals.get("change_pct"),
                "symbol": symbol,
                "instrument_type": market_type,
                "source": "research_pipeline",
            }
            analysis["indicators"] = stage.signals
        elif stage.stage_type == "news":
            analysis["news"] = stage.signals
        elif stage.stage_type == "fundamentals":
            analysis["valuation"] = stage.signals

    return analysis


async def _get_pipeline_result(
    session_id: int, symbol: str, market_type: str
) -> dict[str, Any]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ResearchSession)
            .where(ResearchSession.id == session_id)
            .options(
                selectinload(ResearchSession.summaries),
                selectinload(ResearchSession.stage_analyses),
            )
        )
        session = result.scalar_one_or_none()
        if not session or not session.summaries:
            return {}

        # Get latest summary
        summary = sorted(session.summaries, key=lambda s: s.executed_at, reverse=True)[0]
        return _map_pipeline_to_analysis(session, summary, symbol, market_type)


async def analyze_stock_impl(
    symbol: str,
    market: str | None = None,
    include_peers: bool = False,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    try:
        market_type, normalized_symbol = _resolve_market_type(symbol, market)
    except ValueError:
        raise ValueError(
            f"Unsupported symbol format: '{symbol}'. "
            "Use ticker codes (e.g., AAPL, 005930, KRW-BTC)."
        )

    # ROB-112: Research Pipeline Integration
    if settings.RESEARCH_PIPELINE_ANALYZE_STOCK_ENABLED and settings.RESEARCH_PIPELINE_ENABLED:
        try:
            from app.analysis.pipeline import run_research_session

            async with AsyncSessionLocal() as db:
                session_id = await run_research_session(
                    db=db,
                    symbol=normalized_symbol,
                    name=normalized_symbol,  # TODO: Resolve proper name
                    instrument_type=market_type,
                )
            return await _get_pipeline_result(session_id, normalized_symbol, market_type)
        except Exception as exc:
            logger.warning("research_pipeline.analyze_stock fallback: %s", exc)
            # Fall through to legacy path

    analysis = _build_analysis_payload(normalized_symbol, market_type)
    loop = asyncio.get_running_loop()

    with sentry_sdk.start_span(
        op="analyze_stock.ohlcv_fetch",
        name=f"OHLCV fetch {market_type} {normalized_symbol}",
    ):
        ohlcv_df = await _fetch_ohlcv_for_indicators(
            normalized_symbol, market_type, count=250
        )
    ohlcv_60d = ohlcv_df.tail(60) if len(ohlcv_df) >= 60 else ohlcv_df

    preloaded_quote, named_tasks = _prepare_quote_tasks(
        normalized_symbol,
        market_type,
        ohlcv_df,
    )
    _append_common_tasks(named_tasks, normalized_symbol, ohlcv_df, ohlcv_60d)
    await _append_market_specific_tasks(
        named_tasks, normalized_symbol, market_type, loop
    )
    _append_sector_peers_task(
        named_tasks, normalized_symbol, market_type, include_peers
    )

    with sentry_sdk.start_span(
        op="analyze_stock.gather_tasks",
        name=f"gather tasks {market_type} {normalized_symbol}",
    ):
        task_results = await _gather_task_results(named_tasks)

    with sentry_sdk.start_span(
        op="analyze_stock.assemble_response",
        name=f"assemble response {market_type} {normalized_symbol}",
    ):
        _apply_common_results(analysis, task_results, preloaded_quote)
        _apply_market_specific_results(analysis, task_results, market_type)
        _apply_sector_peers_result(analysis, task_results, market_type, include_peers)
        analysis["errors"] = []
        _apply_recommendation(analysis, market_type)

    return analysis


__all__ = ["analyze_stock_impl"]
