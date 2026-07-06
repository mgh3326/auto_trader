from __future__ import annotations

import argparse
import datetime as dt
from typing import Any

import pytest

from scripts import free_social_sources_probe as cli


def _now() -> dt.datetime:
    return dt.datetime(2026, 7, 6, 1, 2, 3, tzinfo=dt.UTC)


def test_default_sources_are_market_specific() -> None:
    assert cli.default_sources_for_market("kr") == (
        "naver_news",
        "naver_blog",
        "naver_cafe",
        "naver_discussion",
        "bluesky",
    )
    assert cli.default_sources_for_market("us") == ("reddit", "bluesky", "stocktwits")
    assert cli.default_sources_for_market("crypto") == ("reddit", "bluesky")


def test_parser_accepts_comma_separated_sources() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["--market", "us", "--symbol", "AAPL", "--sources", "reddit,bluesky"]
    )
    assert args.market == "us"
    assert args.symbol == "AAPL"
    assert args.sources == "reddit,bluesky"


@pytest.mark.asyncio
async def test_run_probe_builds_social_sentiment_envelope() -> None:
    async def fake_runner(
        source: str,
        *,
        market: str,
        symbol: str,
        query: str,
        limit: int,
        include_x_cdp: bool,
        now: dt.datetime,
    ) -> dict[str, Any]:
        return {
            "source": source,
            "market": market,
            "query": query,
            "status": "ok",
            "observed_at": now.isoformat(),
            "item_count": 1,
            "items": [{"title": f"{source}:{symbol}"}],
        }

    args = argparse.Namespace(
        market="us",
        symbol="AAPL",
        query=None,
        sources="reddit,bluesky",
        limit=5,
        include_x_cdp=False,
    )
    out = await cli.run_probe(args, now=_now(), source_runner=fake_runner)
    assert out["source"] == "free_social_sources_v0"
    assert out["query"] == "AAPL"
    assert out["summary"]["ok_source_count"] == 2
    assert [src["source"] for src in out["sources"]] == ["reddit", "bluesky"]


@pytest.mark.asyncio
async def test_run_probe_keeps_envelope_when_source_runner_raises() -> None:
    async def fake_runner(
        source: str,
        *,
        market: str,
        symbol: str,
        query: str,
        limit: int,
        include_x_cdp: bool,
        now: dt.datetime,
    ) -> dict[str, Any]:
        if source == "reddit":
            raise RuntimeError("reddit down")
        return {
            "source": source,
            "market": market,
            "query": query,
            "status": "ok",
            "observed_at": now.isoformat(),
            "item_count": 1,
            "items": [{"title": f"{source}:{symbol}"}],
        }

    args = argparse.Namespace(
        market="us",
        symbol="AAPL",
        query=None,
        sources="reddit,bluesky",
        limit=5,
        include_x_cdp=False,
    )
    out = await cli.run_probe(args, now=_now(), source_runner=fake_runner)
    assert out["summary"]["source_count"] == 2
    assert out["summary"]["ok_source_count"] == 1
    assert out["sources"][0]["source"] == "reddit"
    assert out["sources"][0]["status"] == "unavailable"
    assert "RuntimeError: reddit down" in out["sources"][0]["error_reason"]
    assert out["sources"][1]["source"] == "bluesky"
    assert out["sources"][1]["status"] == "ok"


@pytest.mark.asyncio
async def test_run_probe_reports_invalid_naver_discussion_symbol_without_crashing() -> (
    None
):
    args = argparse.Namespace(
        market="kr",
        symbol="삼성전자",
        query=None,
        sources="naver_discussion",
        limit=5,
        include_x_cdp=False,
    )
    out = await cli.run_probe(args, now=_now())
    assert out["source"] == "free_social_sources_v0"
    assert out["sources"][0]["source"] == "naver_discussion"
    assert out["sources"][0]["status"] == "invalid_input"
    assert "Korean stocks" in out["sources"][0]["error_reason"]
