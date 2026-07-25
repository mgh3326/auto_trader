"""ROB-686 — KR analyst-consensus Redis cache-aside for the snapshot screener.

Caches the DAILY-STABLE analyst consensus (buy/hold/sell/total counts + target
prices) per (market, symbol, KST-date) so screen_stocks_snapshot stops re-scraping
Naver research pages (company_list/company_read) on every call. The volatile
current_price/upside_pct are stripped before caching and recomputed on the
returned page from a fresh price (see cached_opinion_provider). KR only; US
(yfinance) is not cached. Fail-open + hermetic: reuses app.core.analyze_cache's
Redis client + TTL, guarded by settings.analyze_fetch_cache_enabled.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any

from app.core import analyze_cache
from app.core.timezone import now_kst

logger = logging.getLogger(__name__)

_KEY_PREFIX = "screener_consensus"

_STABLE_CONSENSUS_FIELDS: frozenset[str] = frozenset(
    {
        "buy_count",
        "hold_count",
        "sell_count",
        "strong_buy_count",
        "total_count",
        "avg_target_price",
        "median_target_price",
        "min_target_price",
        "max_target_price",
        "rows_total",
        "rows_used",
        "rows_excluded_stale",
        "rows_undated",
        "newest_opinion_date",
        "window_months",
        "target_price_count",
        "target_price_honest",
    }
)


def _consensus_cache_key(market: str, symbol: str, now: datetime | None = None) -> str:
    date_part = analyze_cache._provider_date_for_key(analyze_cache.PROVIDER_NAVER, now)
    return f"{_KEY_PREFIX}:{analyze_cache.PROVIDER_NAVER}:{symbol.upper()}:{date_part}"


def _strip_volatile(consensus: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in consensus.items() if k in _STABLE_CONSENSUS_FIELDS}


async def get_cached_consensus(
    redis_client: Any, market: str, symbol: str
) -> dict[str, Any] | None:
    if redis_client is None or (market or "").strip().lower() != "kr":
        return None
    key = _consensus_cache_key(market, symbol)
    try:
        raw = await redis_client.get(key)
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("consensus_cache GET failed %s: %s", key, exc)
        return None
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


async def set_cached_consensus(
    redis_client: Any, market: str, symbol: str, consensus: dict[str, Any]
) -> None:
    if redis_client is None or (market or "").strip().lower() != "kr":
        return
    total = consensus.get("total_count")
    if not isinstance(total, int) or total <= 0:
        return  # never cache a degraded/empty consensus
    try:
        now = now_kst()
        ttl = analyze_cache._fetch_cache_ttl_seconds(analyze_cache.PROVIDER_NAVER, now)
        if ttl <= 0:
            return
        serialized = json.dumps(
            _strip_volatile(consensus), default=str, ensure_ascii=False
        )
        await redis_client.set(
            _consensus_cache_key(market, symbol, now), serialized, ex=ttl
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("consensus_cache SET failed %s: %s", symbol, exc)


async def resolve_consensus(
    *,
    symbol: str,
    market: str,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    opinion_fetcher: Any = None,
) -> dict[str, Any] | None:
    market_norm = (market or "").strip().lower()
    memo_key = f"{market_norm}:{symbol.upper()}"
    if memo is not None and memo_key in memo:
        return memo[memo_key]

    if opinion_fetcher is None:
        from app.mcp_server.tooling.fundamentals._valuation import (
            handle_get_investment_opinions,
        )

        opinion_fetcher = handle_get_investment_opinions

    if market_norm == "kr":
        cached = await get_cached_consensus(redis_client, market_norm, symbol)
        if cached is not None:
            if memo is not None:
                memo[memo_key] = cached
            return cached

    stable: dict[str, Any] | None = None
    try:
        # limit=10 preserves the existing filter/page ceiling (see interface note);
        # do NOT bump this — a higher cap triples cold company_read.naver fetches.
        payload = await opinion_fetcher(symbol=symbol, market=market_norm, limit=10)
        consensus = (
            (payload or {}).get("consensus") if isinstance(payload, dict) else None
        )
        if isinstance(consensus, dict) and isinstance(
            consensus.get("total_count"), int
        ):
            if market_norm == "kr":
                await set_cached_consensus(redis_client, market_norm, symbol, consensus)
                stable = _strip_volatile(consensus)
            else:
                stable = consensus  # US: not cached, returned as-is
    except Exception as exc:  # noqa: BLE001 — fail-open
        logger.debug("resolve_consensus live fetch failed %s: %s", symbol, exc)
        stable = None

    if memo is not None:
        memo[memo_key] = stable
    return stable


async def resolve_consensus_counts(
    *,
    symbols: list[str],
    market: str,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    concurrency: int = 4,
    opinion_fetcher: Any = None,
) -> dict[str, dict[str, int | None]]:
    sem = asyncio.Semaphore(max(1, concurrency))  # NOT raised — Naver is throttling
    out: dict[str, dict[str, int | None]] = {}

    async def _one(symbol: str) -> None:
        async with sem:
            stable = await resolve_consensus(
                symbol=symbol,
                market=market,
                redis_client=redis_client,
                memo=memo,
                opinion_fetcher=opinion_fetcher,
            )
            if stable is not None:
                out[symbol] = {
                    "totalCount": stable.get("total_count"),
                    "buyCount": stable.get("buy_count"),
                }

    await asyncio.gather(*(_one(s) for s in dict.fromkeys(symbols)))
    return out


def _recompute_upside(
    consensus: dict[str, Any], price: float | int | None
) -> dict[str, Any]:
    out = dict(consensus)
    avg = out.get("avg_target_price")
    if (
        price
        and isinstance(price, (int, float))
        and avg
        and isinstance(avg, (int, float))
    ):
        out["current_price"] = price
        out["upside_pct"] = round((avg - price) / price * 100, 2)
    else:
        out.pop("current_price", None)
        out.pop("upside_pct", None)
    return out


async def cached_opinion_provider(
    *,
    symbol: str,
    market: str,
    limit: int = 10,
    redis_client: Any = None,
    memo: dict[str, dict[str, Any] | None] | None = None,
    price_fetcher: Any = None,
    opinion_fetcher: Any = None,
) -> dict[str, Any]:
    market_norm = (market or "").strip().lower()
    if market_norm != "kr":
        from app.mcp_server.tooling.fundamentals._valuation import (
            handle_get_investment_opinions,
        )

        return await handle_get_investment_opinions(
            symbol=symbol, market=market_norm, limit=limit
        )

    stable = await resolve_consensus(
        symbol=symbol,
        market=market_norm,
        redis_client=redis_client,
        memo=memo,
        opinion_fetcher=opinion_fetcher,
    )
    if stable is None:
        return {"error": "analyst_consensus_unavailable"}

    if price_fetcher is None:
        from app.services.naver_finance.investor import _fetch_current_price

        price_fetcher = _fetch_current_price
    try:
        price = await price_fetcher(symbol)
    except Exception:  # noqa: BLE001 — fail-open, no stale upside
        price = None

    return {"source": "naver", "consensus": _recompute_upside(stable, price)}
