"""ROB-729 operator CLI for free social/opinion source sampling.

Prints one JSON evidence envelope. No DB writes, no orders, no broker calls.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import os
from collections.abc import Awaitable, Callable
from typing import Any

from app.mcp_server.tooling.fundamentals._retail_sentiment import (
    handle_get_retail_sentiment,
)
from app.services.action_report.remote_debug_audit.cdp_client import CdpClient
from app.services.social_sentiment_probe.bluesky import fetch_bluesky_posts
from app.services.social_sentiment_probe.models import (
    build_social_sentiment_evidence,
    source_result,
)
from app.services.social_sentiment_probe.naver_openapi import fetch_naver_openapi
from app.services.social_sentiment_probe.reddit import fetch_reddit_search
from app.services.social_sentiment_probe.stocktwits import probe_stocktwits_firestream
from app.services.social_sentiment_probe.x_cdp import fetch_x_search_cdp

SourceRunner = Callable[..., Awaitable[dict[str, Any]]]


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, default=str))


def default_sources_for_market(market: str) -> tuple[str, ...]:
    normalized = market.strip().lower()
    if normalized == "kr":
        return ("naver_news", "naver_blog", "naver_cafe", "naver_discussion", "bluesky")
    if normalized == "us":
        return ("reddit", "bluesky", "stocktwits")
    if normalized == "crypto":
        return ("reddit", "bluesky")
    raise ValueError("market must be one of: kr, us, crypto")


def _parse_sources(raw: str | None, market: str) -> tuple[str, ...]:
    if not raw:
        return default_sources_for_market(market)
    return tuple(part.strip() for part in raw.split(",") if part.strip())


async def _run_source(
    source: str,
    *,
    market: str,
    symbol: str,
    query: str,
    limit: int,
    include_x_cdp: bool,
    now: dt.datetime,
) -> dict[str, Any]:
    if source == "naver_news":
        return await fetch_naver_openapi(
            "news",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_blog":
        return await fetch_naver_openapi(
            "blog",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_cafe":
        return await fetch_naver_openapi(
            "cafearticle",
            query,
            market,
            os.getenv("NAVER_CLIENT_ID"),
            os.getenv("NAVER_CLIENT_SECRET"),
            display=limit,
            now=now,
        )
    if source == "naver_discussion":
        if market != "kr":
            return source_result(
                source="naver_discussion",
                market=market,
                query=query,
                status="unsupported_market",
                items=[],
                observed_at=now,
                error_reason="Naver discussion aggregate signal supports KR only",
            )
        payload = await handle_get_retail_sentiment(symbol, market="kr")
        return source_result(
            source="naver_discussion",
            market=market,
            query=query,
            status=payload.get("status", "unavailable"),
            items=[payload] if payload.get("status") == "ok" else [],
            observed_at=now,
            error_reason=payload.get("note") or payload.get("error"),
        )
    if source == "reddit":
        return await fetch_reddit_search(
            query,
            market,
            os.getenv("REDDIT_CLIENT_ID"),
            os.getenv("REDDIT_CLIENT_SECRET"),
            os.getenv("REDDIT_USER_AGENT"),
            subreddits=("stocks", "wallstreetbets")
            if market == "us"
            else ("CryptoCurrency",),
            limit=limit,
            now=now,
        )
    if source == "bluesky":
        return await fetch_bluesky_posts(query, market, limit=limit, now=now)
    if source == "stocktwits":
        return probe_stocktwits_firestream(
            symbol,
            market,
            os.getenv("STOCKTWITS_FIRESTREAM_USERNAME"),
            os.getenv("STOCKTWITS_FIRESTREAM_PASSWORD"),
            now=now,
        )
    if source == "x_cdp":
        if not include_x_cdp:
            return source_result(
                source="x_cdp",
                market=market,
                query=query,
                status="disabled",
                items=[],
                observed_at=now,
                error_reason="pass --include-x-cdp to use the local Chrome session",
            )
        return await fetch_x_search_cdp(
            query, market, CdpClient(), limit=limit, now=now
        )
    return source_result(
        source=source,
        market=market,
        query=query,
        status="unknown_source",
        items=[],
        observed_at=now,
        error_reason=f"unknown source: {source}",
    )


async def run_probe(
    args: argparse.Namespace,
    *,
    now: dt.datetime | None = None,
    source_runner: SourceRunner | None = None,
) -> dict[str, Any]:
    observed_at = now or dt.datetime.now(dt.UTC)
    market = args.market.strip().lower()
    symbol = args.symbol.strip()
    query = (args.query or symbol).strip()
    runner = source_runner or _run_source
    results = []
    for source in _parse_sources(args.sources, market):
        results.append(
            await runner(
                source,
                market=market,
                symbol=symbol,
                query=query,
                limit=args.limit,
                include_x_cdp=args.include_x_cdp,
                now=observed_at,
            )
        )
    return build_social_sentiment_evidence(
        market=market,
        symbol=symbol,
        query=query,
        source_results=results,
        observed_at=observed_at,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Free social/opinion source probe (ROB-729, operator-only)"
    )
    parser.add_argument("--market", required=True, choices=["kr", "us", "crypto"])
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--query", default=None)
    parser.add_argument("--sources", default=None)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--include-x-cdp", action="store_true")
    return parser


async def _amain(args: argparse.Namespace) -> int:
    _emit(await run_probe(args))
    return 0


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
