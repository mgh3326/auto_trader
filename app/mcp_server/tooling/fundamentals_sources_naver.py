"""Naver provider helpers for fundamentals and analysis tools."""

from __future__ import annotations

import asyncio
import datetime
import json
from typing import Any

import httpx

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling.fundamentals_sources_common import (
    _fetch_screen_enrichment_payload,
)
from app.mcp_server.tooling.fundamentals_sources_finnhub import (
    _fetch_company_profile_finnhub,
)
from app.services import naver_finance


async def _fetch_news_naver(symbol: str, limit: int) -> dict[str, Any]:
    news_items = await naver_finance.fetch_news(symbol, limit=limit)
    return {
        "symbol": symbol,
        "market": "kr",
        "source": "naver",
        "count": len(news_items),
        "news": news_items,
    }


async def _fetch_analysis_snapshot_naver(
    symbol: str,
    news_limit: int,
    opinions_limit: int,
) -> dict[str, Any]:
    snapshot = await naver_finance._fetch_kr_snapshot(
        symbol,
        news_limit=news_limit,
        opinion_limit=opinions_limit,
    )
    result: dict[str, Any] = {}
    valuation = snapshot.get("valuation")
    if isinstance(valuation, dict):
        result["valuation"] = {
            "instrument_type": "equity_kr",
            "source": "naver",
            **valuation,
        }

    news_items = snapshot.get("news")
    if isinstance(news_items, list):
        result["news"] = {
            "symbol": symbol,
            "market": "kr",
            "source": "naver",
            "count": len(news_items),
            "news": news_items,
        }

    opinions = snapshot.get("opinions")
    if isinstance(opinions, dict):
        result["opinions"] = {
            "instrument_type": "equity_kr",
            "source": "naver",
            **opinions,
        }

    return result


async def _fetch_company_profile_naver(symbol: str) -> dict[str, Any]:
    profile = await naver_finance.fetch_company_profile(symbol)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **profile,
    }


async def _fetch_financials_naver(
    symbol: str, statement: str, freq: str
) -> dict[str, Any]:
    financials = await naver_finance.fetch_financials(symbol, statement, freq)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **financials,
    }


async def _fetch_investor_trends_naver(symbol: str, days: int) -> dict[str, Any]:
    trends = await naver_finance.fetch_investor_trends(symbol, days=days)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **trends,
    }


async def _fetch_investment_opinions_naver(symbol: str, limit: int) -> dict[str, Any]:
    opinions = await naver_finance.fetch_investment_opinions(symbol, limit=limit)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **opinions,
    }


async def _fetch_screen_enrichment_kr(symbol: str) -> dict[str, Any]:
    return await _fetch_screen_enrichment_payload(
        symbol=symbol,
        profile_request=_fetch_company_profile_finnhub(symbol),
        opinions_request=_fetch_investment_opinions_naver(symbol, 10),
        profile_provider="finnhub",
        opinions_provider="naver",
    )


async def _fetch_valuation_naver(symbol: str) -> dict[str, Any]:
    valuation = await naver_finance.fetch_valuation(symbol)
    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        **valuation,
    }


async def _fetch_sector_peers_naver(
    symbol: str, limit: int, manual_peers: list[str] | None = None
) -> dict[str, Any]:
    data = await naver_finance.fetch_sector_peers(symbol, limit=limit)
    peers = data["peers"]

    target_per = data.get("per")
    target_pbr = data.get("pbr")

    all_pers = [
        v
        for v in [target_per] + [p.get("per") for p in peers]
        if v is not None and v > 0
    ]
    all_pbrs = [
        v
        for v in [target_pbr] + [p.get("pbr") for p in peers]
        if v is not None and v > 0
    ]

    avg_per = round(sum(all_pers) / len(all_pers), 2) if all_pers else None
    avg_pbr = round(sum(all_pbrs) / len(all_pbrs), 2) if all_pbrs else None

    target_per_rank = None
    if target_per is not None and target_per > 0 and all_pers:
        sorted_pers = sorted(all_pers)
        target_per_rank = f"{sorted_pers.index(target_per) + 1}/{len(sorted_pers)}"

    target_pbr_rank = None
    if target_pbr is not None and target_pbr > 0 and all_pbrs:
        sorted_pbrs = sorted(all_pbrs)
        target_pbr_rank = f"{sorted_pbrs.index(target_pbr) + 1}/{len(sorted_pbrs)}"

    return {
        "instrument_type": "equity_kr",
        "source": "naver",
        "symbol": symbol,
        "name": data.get("name"),
        "sector": data.get("sector"),
        "current_price": data.get("current_price"),
        "change_pct": data.get("change_pct"),
        "per": target_per,
        "pbr": target_pbr,
        "market_cap": data.get("market_cap"),
        "peers": peers,
        "comparison": {
            "avg_per": avg_per,
            "avg_pbr": avg_pbr,
            "target_per_rank": target_per_rank,
            "target_pbr_rank": target_pbr_rank,
        },
    }


# ---------------------------------------------------------------------------
# Kimchi Premium Helpers
# ---------------------------------------------------------------------------

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
EXCHANGE_RATE_URL = "https://open.er-api.com/v6/latest/USD"


async def _fetch_exchange_rate_usd_krw() -> float:
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(EXCHANGE_RATE_URL)
        r.raise_for_status()
        data = r.json()
        return float(data["rates"]["KRW"])


async def _fetch_binance_prices(symbols: list[str]) -> dict[str, float]:
    pairs = [f"{s}USDT" for s in symbols]
    async with httpx.AsyncClient(timeout=10) as cli:
        symbols_json = json.dumps(pairs, separators=(",", ":"))
        r = await cli.get(BINANCE_TICKER_URL, params={"symbols": symbols_json})
        r.raise_for_status()
        data = r.json()

    result: dict[str, float] = {}
    for item in data:
        pair: str = item["symbol"]
        if pair.endswith("USDT"):
            sym = pair[: -len("USDT")]
            result[sym] = float(item["price"])
    return result


async def _fetch_kimchi_premium(symbols: list[str]) -> dict[str, Any]:
    upbit_markets = [f"KRW-{s}" for s in symbols]

    upbit_prices, binance_prices, exchange_rate = await asyncio.gather(
        upbit_service.fetch_multiple_current_prices(upbit_markets),
        _fetch_binance_prices(symbols),
        _fetch_exchange_rate_usd_krw(),
    )

    data: list[dict[str, Any]] = []
    for sym in symbols:
        upbit_key = f"KRW-{sym}"
        upbit_krw = upbit_prices.get(upbit_key)
        binance_usdt = binance_prices.get(sym)

        if upbit_krw is None or binance_usdt is None:
            continue

        binance_krw = binance_usdt * exchange_rate
        premium_pct = round((upbit_krw - binance_krw) / binance_krw * 100, 2)

        data.append(
            {
                "symbol": sym,
                "upbit_krw": upbit_krw,
                "binance_usdt": binance_usdt,
                "binance_krw": round(binance_krw, 0),
                "premium_pct": premium_pct,
            }
        )

    now = datetime.datetime.now(tz=datetime.UTC).strftime("%Y-%m-%dT%H:%M:%S")

    return {
        "instrument_type": "crypto",
        "source": "upbit+binance",
        "timestamp": now,
        "exchange_rate": exchange_rate,
        "count": len(data),
        "data": data,
    }


__all__ = [
    "_fetch_analysis_snapshot_naver",
    "_fetch_company_profile_naver",
    "_fetch_financials_naver",
    "_fetch_investment_opinions_naver",
    "_fetch_investor_trends_naver",
    "_fetch_kimchi_premium",
    "_fetch_news_naver",
    "_fetch_screen_enrichment_kr",
    "_fetch_sector_peers_naver",
    "_fetch_valuation_naver",
]
