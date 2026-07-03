from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import sentry_sdk
import yfinance as yf

from app.core import analyze_cache
from app.core.timezone import now_kst
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
from app.mcp_server.tooling.market_data_indicators import (
    _fetch_ohlcv_for_indicators,
    _split_support_resistance_levels,
)
from app.mcp_server.tooling.market_data_quotes import (
    _fetch_kr_live_quote,
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
from app.monitoring import build_yfinance_tracing_session, close_yfinance_session
from app.services.symbol_analysis.floor import floored_action, insufficient_inputs
from app.services.kr_symbol_universe_service import get_kr_nxt_tradability
from app.services.symbol_analysis.freshness import compute_is_stale

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


_KST = ZoneInfo("Asia/Seoul")


async def _resolve_kr_quote(
    symbol: str, ohlcv_df: pd.DataFrame
) -> dict[str, Any] | None:
    """KR analyze quote: 라이브 inquire_price 우선, 실패 시 일봉 종가 fallback.
    두 경로 모두 price_as_of + is_stale_price 를 정직하게 태그한다."""
    trading_date = datetime.now(_KST).date()

    async def _annotate(quote: dict[str, Any]) -> dict[str, Any]:
        tradability = (await get_kr_nxt_tradability([symbol])).get(symbol)
        if tradability is not None:
            quote.update(tradability.public_fields())
        return quote

    live = await _fetch_kr_live_quote(symbol)
    if live is not None:
        as_of_raw = live.get("price_as_of")
        as_of_dt = datetime.fromisoformat(as_of_raw) if as_of_raw else None
        live["is_stale_price"] = compute_is_stale(
            "price", as_of_dt, trading_date=trading_date
        )
        return await _annotate(live)

    fallback = _build_kr_quote_from_ohlcv(symbol, ohlcv_df)
    if fallback is None:
        return None
    last_idx = ohlcv_df.index[-1]
    as_of_dt = pd.Timestamp(last_idx).to_pydatetime()
    fallback["price_as_of"] = as_of_dt.isoformat()
    fallback["is_stale_price"] = compute_is_stale(
        "price", as_of_dt, trading_date=trading_date
    )
    return await _annotate(fallback)


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
        named_tasks.append(
            (
                "quote",
                asyncio.create_task(_resolve_kr_quote(normalized_symbol, ohlcv_df)),
            )
        )
        return None, named_tasks

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


# Task names whose results carry the fetch-cache envelope
# ({"payload": ..., "cache_hit": bool, "fetched_at": iso}) — ROB-638.
_FETCH_CACHE_TASK_NAMES = ("kr_snapshot", "us_yf_bundle", "profile")


def _fetch_cache_envelope(
    payload: dict[str, Any],
    *,
    cache_hit: bool,
    fetched_at: str | None,
) -> dict[str, Any]:
    return {"payload": payload, "cache_hit": cache_hit, "fetched_at": fetched_at}


async def _fetch_kr_snapshot_cached(
    normalized_symbol: str, refresh: bool
) -> dict[str, Any]:
    """KR Naver snapshot (valuation/news/opinions) behind the fetch-layer cache.

    ROB-638: only the slowly-changing provider fetch is cached — quote/RSI/S&R
    and the recommendation are recomputed by the caller on EVERY call. Degraded
    (empty) snapshots are never cached; a fetch exception propagates (and is
    swallowed by ``_gather_task_results``) without touching the cache.
    """
    redis_client = await analyze_cache._get_redis_client()
    if not refresh:
        payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
            redis_client, analyze_cache.PROVIDER_NAVER, normalized_symbol
        )
        if payload is not None:
            return _fetch_cache_envelope(payload, cache_hit=True, fetched_at=fetched_at)

    snapshot = await _fetch_analysis_snapshot_naver(normalized_symbol, 5, 10)
    fetched_at = now_kst().isoformat()
    if isinstance(snapshot, dict) and snapshot:
        # refresh=True still WRITES the fresh value — it only bypasses the read.
        await analyze_cache.set_cached_fetch_payload(
            redis_client,
            analyze_cache.PROVIDER_NAVER,
            normalized_symbol,
            snapshot,
            fetched_at=fetched_at,
        )
    return _fetch_cache_envelope(snapshot, cache_hit=False, fetched_at=fetched_at)


async def _fetch_us_yf_bundle(
    normalized_symbol: str,
    yf_ticker: Any,
    yf_session: Any,
    loop: asyncio.AbstractEventLoop,
    redis_client: Any,
) -> dict[str, Any]:
    """Fresh US yfinance snapshot → valuation + opinions bundle (cache MISS path).

    The snapshot collection runs in the executor inside this task so it overlaps
    the other provider fetches. The bundle is cached only when fully healthy:
    a snapshot where every sub-fetch failed (all None) or a partial bundle
    (valuation OR opinions raised) is returned fresh but never cached.
    """
    yf_snapshot = await loop.run_in_executor(
        None, _collect_yfinance_snapshot, yf_ticker
    )
    bundle: dict[str, Any] = {}
    part_errors = 0
    try:
        bundle["valuation"] = await _fetch_valuation_yfinance(
            normalized_symbol, snapshot=yf_snapshot, session=yf_session
        )
    except Exception:
        part_errors += 1
    try:
        bundle["opinions"] = await _fetch_investment_opinions_yfinance(
            normalized_symbol, 10, snapshot=yf_snapshot, session=yf_session
        )
    except Exception:
        part_errors += 1

    fetched_at = now_kst().isoformat()
    snapshot_degraded = (
        yf_snapshot.info is None
        and yf_snapshot.analyst_price_targets is None
        and yf_snapshot.recommendations is None
        and yf_snapshot.upgrades_downgrades is None
    )
    if not snapshot_degraded and part_errors == 0:
        await analyze_cache.set_cached_fetch_payload(
            redis_client,
            analyze_cache.PROVIDER_YFINANCE,
            normalized_symbol,
            bundle,
            fetched_at=fetched_at,
        )
    return _fetch_cache_envelope(bundle, cache_hit=False, fetched_at=fetched_at)


async def _fetch_us_profile_cached(
    normalized_symbol: str, refresh: bool, redis_client: Any
) -> dict[str, Any]:
    """US Finnhub company profile behind the fetch-layer cache.

    ``_fetch_company_profile_finnhub`` raises on a missing profile, so a
    degraded fetch propagates (swallowed by ``_gather_task_results``) and is
    never cached.
    """
    if not refresh:
        payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
            redis_client, analyze_cache.PROVIDER_FINNHUB_PROFILE, normalized_symbol
        )
        if payload is not None:
            return _fetch_cache_envelope(payload, cache_hit=True, fetched_at=fetched_at)

    profile = await _fetch_company_profile_finnhub(normalized_symbol)
    fetched_at = now_kst().isoformat()
    await analyze_cache.set_cached_fetch_payload(
        redis_client,
        analyze_cache.PROVIDER_FINNHUB_PROFILE,
        normalized_symbol,
        profile,
        fetched_at=fetched_at,
    )
    return _fetch_cache_envelope(profile, cache_hit=False, fetched_at=fetched_at)


async def _append_market_specific_tasks(
    named_tasks: list[tuple[str, asyncio.Task[Any]]],
    normalized_symbol: str,
    market_type: str,
    loop: asyncio.AbstractEventLoop,
    refresh: bool = False,
) -> Any | None:
    if market_type == "equity_kr":
        named_tasks.append(
            (
                "kr_snapshot",
                asyncio.create_task(
                    _fetch_kr_snapshot_cached(normalized_symbol, refresh)
                ),
            )
        )
        return None

    if market_type == "crypto":
        # Crypto is NEVER cached (no analyst-consensus source) — this branch
        # must not touch the cache client at all (asserted by tests).
        named_tasks.append(
            (
                "news",
                asyncio.create_task(
                    _fetch_news_finnhub(normalized_symbol, "crypto", 5)
                ),
            )
        )
        return None

    redis_client = await analyze_cache._get_redis_client()
    yf_session = None
    bundle_envelope: dict[str, Any] | None = None
    if not refresh:
        payload, fetched_at = await analyze_cache.get_cached_fetch_payload(
            redis_client, analyze_cache.PROVIDER_YFINANCE, normalized_symbol
        )
        if payload is not None:
            bundle_envelope = _fetch_cache_envelope(
                payload, cache_hit=True, fetched_at=fetched_at
            )

    if bundle_envelope is not None:
        # Cache hit — no yfinance session/ticker is built at all.
        async def _cached_bundle(
            envelope: dict[str, Any] = bundle_envelope,
        ) -> dict[str, Any]:
            return envelope

        named_tasks.append(("us_yf_bundle", asyncio.create_task(_cached_bundle())))
    else:
        yf_session = build_yfinance_tracing_session()
        yf_ticker = yf.Ticker(normalized_symbol, session=yf_session)
        named_tasks.append(
            (
                "us_yf_bundle",
                asyncio.create_task(
                    _fetch_us_yf_bundle(
                        normalized_symbol, yf_ticker, yf_session, loop, redis_client
                    )
                ),
            )
        )

    named_tasks.extend(
        [
            (
                "profile",
                asyncio.create_task(
                    _fetch_us_profile_cached(normalized_symbol, refresh, redis_client)
                ),
            ),
            (
                "news",
                asyncio.create_task(_fetch_news_finnhub(normalized_symbol, "us", 5)),
            ),
        ]
    )
    return yf_session


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


def _to_optional_price(value: Any) -> float | None:
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _recompute_intraday_support_resistance(
    analysis: dict[str, Any],
    market_type: str,
) -> None:
    """ROB-541: re-sign S/R level distances against the LIVE quote price.

    The EOD support_resistance payload computes ``distance_pct`` and the
    support/resistance split against the daily-close ``current_price``. On an
    intraday gap, that misclassifies levels relative to where the symbol is
    actually trading. For KR/crypto (which carry a live ``quote.price``), we
    recompute each level's ``distance_pct`` AND re-split supports vs resistances
    against the live price.

    The EOD S/R price LEVELS themselves are left intact — only their distance
    and bucket are recomputed. ``_support_resistance.py`` (shared with the
    standalone get_support_resistance tool) is NOT touched.
    """
    if market_type not in {"equity_kr", "crypto"}:
        return
    sr = analysis.get("support_resistance")
    if not isinstance(sr, dict) or "error" in sr:
        return
    quote = analysis.get("quote") or {}
    live_price = _to_optional_price(quote.get("price") or quote.get("current_price"))
    if live_price is None:
        return

    levels = [*(sr.get("supports") or []), *(sr.get("resistances") or [])]
    if not levels:
        return

    # Pass copies so the EOD price levels are preserved; the splitter mutates
    # distance_pct in place, which is exactly the intraday recompute we want.
    recomputed = [dict(level) for level in levels]
    supports, resistances = _split_support_resistance_levels(recomputed, live_price)
    sr["supports"] = supports
    sr["resistances"] = resistances
    sr["distance_basis_price"] = round(live_price, 2)
    sr["distance_basis"] = "live_quote"


def _envelope_payload(task_results: dict[str, Any], name: str) -> dict[str, Any] | None:
    """Unwrap a fetch-cache envelope task result into its provider payload."""
    envelope = task_results.get(name)
    if not isinstance(envelope, dict):
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        return None
    return payload


def _apply_kr_results(analysis: dict[str, Any], task_results: dict[str, Any]) -> None:
    kr_snapshot = _envelope_payload(task_results, "kr_snapshot")
    if kr_snapshot is None:
        return
    for key in ("valuation", "news", "opinions"):
        if key in kr_snapshot:
            analysis[key] = kr_snapshot[key]


def _apply_us_results(analysis: dict[str, Any], task_results: dict[str, Any]) -> None:
    bundle = _envelope_payload(task_results, "us_yf_bundle")
    if bundle is not None:
        for key in ("valuation", "opinions"):
            if key in bundle:
                analysis[key] = bundle[key]
    profile = _envelope_payload(task_results, "profile")
    if profile:
        analysis["profile"] = profile
    if "news" in task_results:
        analysis["news"] = task_results["news"]


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


def _apply_fetch_cache_metadata(
    analysis: dict[str, Any], task_results: dict[str, Any]
) -> None:
    """Attach the ROB-638 fetch-cache response contract keys.

    ``cache_hit`` — True when the fetch-layer cache served ANY provider payload
    for this symbol. ``derived_as_of`` — ISO-KST timestamp of when the (cached
    or fresh) provider data was fetched; with multiple providers the OLDEST
    fetch time is reported (most conservative freshness statement). Crypto and
    fully-fresh runs report the current fetch time.
    """
    envelopes = [
        envelope
        for name in _FETCH_CACHE_TASK_NAMES
        if isinstance((envelope := task_results.get(name)), dict)
        and "payload" in envelope
    ]
    analysis["cache_hit"] = any(env.get("cache_hit") for env in envelopes)
    fetched_ats = sorted(
        str(env["fetched_at"]) for env in envelopes if env.get("fetched_at")
    )
    analysis["derived_as_of"] = fetched_ats[0] if fetched_ats else now_kst().isoformat()


def _apply_sector_peers_result(
    analysis: dict[str, Any],
    task_results: dict[str, Any],
    market_type: str,
    include_peers: bool,
) -> None:
    if include_peers and market_type != "crypto" and "sector_peers" in task_results:
        analysis["sector_peers"] = task_results["sector_peers"]


def _consensus_rows_present(consensus: Any) -> bool:
    """ROB-486: stale-only 컨센서스가 presence 플로어를 통과하지 못하게 한다.

    KR(naver) windowed consensus 는 rows_used(윈도우 생존 row 수) 기준,
    US(yfinance) consensus 는 rows_used 가 없으므로 total_count 기준.
    둘 다 없거나 0 이면 consensus 부재로 본다 (fail-closed).
    """
    if not isinstance(consensus, dict) or not consensus:
        return False
    rows_used = consensus.get("rows_used")
    if rows_used is not None:
        try:
            return int(rows_used) > 0
        except (TypeError, ValueError):
            return False
    total = consensus.get("total_count")
    if total is None:
        return False
    try:
        return int(total) > 0
    except (TypeError, ValueError):
        return False


def _apply_recommendation(
    analysis: dict[str, Any],
    market_type: str,
) -> None:
    if market_type not in ("equity_kr", "equity_us"):
        return

    recommendation = _build_recommendation_for_equity(analysis, market_type)

    quote = analysis.get("quote") or {}
    price_present = quote.get("price") is not None
    consensus_present = _consensus_rows_present(
        (analysis.get("opinions") or {}).get("consensus")
    )

    if recommendation is None:
        # price/quote 부재 → unavailable floor 레코멘데이션을 정직하게 부착.
        recommendation = {
            "action": "hold",
            "confidence": "low",
            "rsi14": None,
            "buy_zones": [],
            "sell_targets": [],
            "stop_loss": None,
            "reasoning": "",
        }
    rsi_present = recommendation.get("rsi14") is not None

    missing = insufficient_inputs(
        price_present=price_present,
        rsi_present=rsi_present,
        consensus_present=consensus_present,
    )
    action, confidence = floored_action(
        recommendation["action"], recommendation["confidence"], insufficient=missing
    )
    recommendation["action"] = action
    recommendation["confidence"] = confidence
    recommendation["insufficient_inputs"] = missing

    analysis["recommendation"] = recommendation


async def analyze_stock_impl(
    symbol: str,
    market: str | None = None,
    include_peers: bool = False,
    refresh: bool = False,
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
    yfinance_session_to_close = await _append_market_specific_tasks(
        named_tasks, normalized_symbol, market_type, loop, refresh=refresh
    )
    _append_sector_peers_task(
        named_tasks, normalized_symbol, market_type, include_peers
    )

    try:
        with sentry_sdk.start_span(
            op="analyze_stock.gather_tasks",
            name=f"gather tasks {market_type} {normalized_symbol}",
        ):
            task_results = await _gather_task_results(named_tasks)
    finally:
        if yfinance_session_to_close is not None:
            close_yfinance_session(yfinance_session_to_close)

    with sentry_sdk.start_span(
        op="analyze_stock.assemble_response",
        name=f"assemble response {market_type} {normalized_symbol}",
    ):
        _apply_common_results(analysis, task_results, preloaded_quote)
        # ROB-541 — re-sign S/R level distances against the live quote (KR/crypto).
        _recompute_intraday_support_resistance(analysis, market_type)
        _apply_market_specific_results(analysis, task_results, market_type)
        _apply_sector_peers_result(analysis, task_results, market_type, include_peers)
        # ROB-638 — fetch-cache response contract (cache_hit / derived_as_of).
        _apply_fetch_cache_metadata(analysis, task_results)
        analysis["errors"] = []
        _apply_recommendation(analysis, market_type)

    return analysis


__all__ = ["analyze_stock_impl"]
