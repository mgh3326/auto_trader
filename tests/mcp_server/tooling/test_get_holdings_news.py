# tests/mcp_server/tooling/test_get_holdings_news.py
"""get_holdings_news cross-market sweep (ROB-628 P2)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.mcp_server.tooling import portfolio_holdings
from app.mcp_server.tooling.fundamentals import _news
from app.services import symbol_news_service
from app.services.symbol_news_service import (
    SymbolNewsArticle,
    SymbolNewsFetchResult,
)


def _article(symbol: str, market: str, idx: int) -> SymbolNewsArticle:
    url = f"https://news/{symbol}/{idx}"
    return SymbolNewsArticle(
        provider="naver" if market == "kr" else "finnhub",
        market=market,
        symbol=symbol,
        external_article_id=f"{symbol}-{idx}",
        title=f"{symbol} headline {idx}",
        source_name="한국경제" if market == "kr" else "Reuters",
        canonical_url=url,
        summary=None,
        published_at=datetime(2026, 6, 20, 9, idx, tzinfo=UTC),
        fetched_at=datetime(2026, 6, 20, 10, 0, tzinfo=UTC),
        provider_metadata={
            "source_item": {"title": f"{symbol} headline {idx}", "url": url},
            "relevance": {"status": "pending"},
        },
    )


def _ok_result(symbol: str, market: str, n: int = 1) -> SymbolNewsFetchResult:
    provider = "naver" if market == "kr" else "finnhub"
    arts = [_article(symbol, market, i) for i in range(n)]
    return SymbolNewsFetchResult(
        symbol, market, provider, "ok", 5, n, arts, excluded_count=0
    )


def _patch_fetch(monkeypatch, fn) -> None:
    monkeypatch.setattr(symbol_news_service, "fetch_symbol_news", fn)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_symbols_passed_through(monkeypatch) -> None:
    calls: list[tuple[str, str, str | None, int]] = []

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        calls.append((symbol, market, instrument_type, limit))
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["005930", "AAPL", "KRW-BTC"], limit_per_symbol=5
    )

    # passed through (normalized) and not re-resolved from holdings
    assert out["symbols_requested"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["symbols_resolved"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["count"] == 3
    assert "degraded_reason" not in out
    # market inferred per symbol + correct instrument_type + limit threaded
    assert {(c[1], c[2]) for c in calls} == {
        ("kr", "equity_kr"),
        ("us", "equity_us"),
        ("crypto", "crypto"),
    }
    assert all(c[3] == 5 for c in calls)
    # name unknown for passed-through symbols
    assert all(row["name"] is None for row in out["results"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_result_row_shape_is_lean(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market, n=2)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=["AAPL"], limit_per_symbol=5)

    row = out["results"][0]
    assert set(row.keys()) == {"symbol", "name", "market", "status", "news"}
    assert row["symbol"] == "AAPL"
    assert row["market"] == "us"
    assert row["status"] == "ok"
    assert len(row["news"]) == 2
    item = row["news"][0]
    assert set(item.keys()) == {
        "title",
        "url",
        "source",
        "published_at",
        "relevance",
    }
    assert item["title"] == "AAPL headline 0"
    assert item["url"] == "https://news/AAPL/0"
    assert item["source"] == "Reuters"
    assert item["published_at"].startswith("2026-06-20T09:00")
    assert item["relevance"] == {"status": "pending"}


@pytest.mark.unit
@pytest.mark.asyncio
async def test_omitted_symbols_resolves_holdings(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_collect(**kwargs):
        captured.update(kwargs)
        return (
            [
                {"symbol": "005930", "market": "kr", "name": "삼성전자"},
                {"symbol": "AAPL", "market": "us", "name": "Apple"},
                {"symbol": "KRW-BTC", "market": "crypto", "name": "비트코인"},
                # duplicate (same symbol+market, different account) -> de-duped
                {"symbol": "005930", "market": "kr", "name": "삼성전자"},
                # junk market -> dropped
                {"symbol": "XXX", "market": "", "name": "junk"},
            ],
            [],
            None,
            None,
        )

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    # holdings resolver invoked with the cheap (no-price) read
    assert captured["account"] is None
    assert captured["market"] is None
    assert captured["include_current_price"] is False
    # de-duped + junk dropped, names carried through from holdings
    assert out["symbols_resolved"] == ["005930", "AAPL", "KRW-BTC"]
    assert out["count"] == 3
    names = {r["symbol"]: r["name"] for r in out["results"]}
    assert names == {"005930": "삼성전자", "AAPL": "Apple", "KRW-BTC": "비트코인"}
    assert "degraded_reason" not in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_caps_symbols_at_max_with_degraded(monkeypatch) -> None:
    big = [f"{i:06d}" for i in range(35)]  # 35 distinct KR 6-digit codes

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=big, limit_per_symbol=5)

    assert len(out["symbols_requested"]) == 35
    assert len(out["symbols_resolved"]) == _news.HOLDINGS_NEWS_MAX_SYMBOLS
    assert out["count"] == _news.HOLDINGS_NEWS_MAX_SYMBOLS
    assert "degraded_reason" in out
    assert str(_news.HOLDINGS_NEWS_MAX_SYMBOLS) in out["degraded_reason"]
    # nothing fabricated/dropped silently: resolved is a prefix of requested
    assert (
        out["symbols_resolved"]
        == out["symbols_requested"][: _news.HOLDINGS_NEWS_MAX_SYMBOLS]
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_mode_sorts_by_priority_before_cap(monkeypatch) -> None:
    """ROB-889: holdings-mode candidates are ordered by cost-basis weight (desc)
    with order_routable as the tiebreaker — not the arbitrary alphabetical
    (account, market, symbol) order that let ETFs crowd out big positions."""

    async def fake_collect(**kwargs):
        return (
            [
                # alphabetically first, tiny position -> must NOT lead anymore
                {
                    "symbol": "000010",
                    "market": "kr",
                    "name": "Tiny ETF",
                    "source": "kis",
                    "broker": "kis",
                    "quantity": 1,
                    "avg_buy_price": 1000,  # weight 1_000
                },
                # alphabetically last, biggest conviction -> must lead
                {
                    "symbol": "999990",
                    "market": "kr",
                    "name": "Big conviction",
                    "source": "kis",
                    "broker": "kis",
                    "quantity": 100,
                    "avg_buy_price": 1000,  # weight 100_000
                },
                # mid weight, routable
                {
                    "symbol": "500000",
                    "market": "kr",
                    "name": "Mid",
                    "source": "kis",
                    "broker": "kis",
                    "quantity": 10,
                    "avg_buy_price": 1000,  # weight 10_000
                },
            ],
            [],
            None,
            None,
        )

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    # weight desc: 999990 (100k) > 500000 (10k) > 000010 (1k)
    assert out["symbols_resolved"] == ["999990", "500000", "000010"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_cap_keeps_high_priority_over_front_etfs(monkeypatch) -> None:
    """ROB-889: a big watched name placed LAST in the raw holdings order must
    survive the 30-cap; a tiny front ETF is the one dropped. (Pre-fix the head
    slice kept the front and dropped the watched name.)"""
    etfs = [
        {
            "symbol": f"{i:06d}",
            "market": "kr",
            "name": f"ETF {i}",
            "source": "kis",
            "broker": "kis",
            "quantity": 1,
            "avg_buy_price": 1000,  # weight 1_000 each
        }
        for i in range(1, 31)  # 000001..000030 — 30 tiny ETFs, front of the list
    ]
    watched = {
        "symbol": "999999",
        "market": "kr",
        "name": "Watched big",
        "source": "kis",
        "broker": "kis",
        "quantity": 1000,
        "avg_buy_price": 1000,  # weight 1_000_000 — last in list
    }

    async def fake_collect(**kwargs):
        return (etfs + [watched], [], None, None)

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    resolved = out["symbols_resolved"]
    assert len(resolved) == _news.HOLDINGS_NEWS_MAX_SYMBOLS
    # the big watched name survived and now leads
    assert resolved[0] == "999999"
    assert "999999" in resolved
    # exactly one tiny ETF got dropped (the lowest-priority by symbol tiebreak)
    assert "000030" not in resolved
    assert "degraded_reason" in out


@pytest.mark.unit
@pytest.mark.asyncio
async def test_explicit_symbols_mode_preserves_request_order(monkeypatch) -> None:
    """ROB-889: priority sort applies ONLY to holdings mode. An explicit symbols
    list is the caller's own priority order and must be preserved verbatim."""

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["999990", "000010", "500000"], limit_per_symbol=5
    )

    assert out["symbols_resolved"] == ["999990", "000010", "500000"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_per_symbol_failure_is_fail_soft(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        if symbol == "AAPL":
            raise RuntimeError("boom")
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["005930", "AAPL", "TSLA"], limit_per_symbol=5
    )

    assert out["count"] == 3  # one bad symbol did not kill the sweep
    by_symbol = {r["symbol"]: r for r in out["results"]}
    assert by_symbol["AAPL"]["status"] == "error"
    assert by_symbol["AAPL"]["degraded_reason"] == "RuntimeError"
    assert by_symbol["AAPL"]["news"] == []
    # neighbours unaffected
    assert by_symbol["005930"]["news"]
    assert by_symbol["TSLA"]["news"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_row_surfaces_result_degraded(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return SymbolNewsFetchResult(
            symbol,
            market,
            "finnhub",
            "ok",
            5,
            1,
            [_article(symbol, market, 0)],
            degraded=True,
            fetch_error="TimeoutError",
        )

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=["AAPL"], limit_per_symbol=5)

    row = out["results"][0]
    assert row["status"] == "ok"
    assert row["degraded_reason"] == "TimeoutError"
    assert row["news"]  # cached articles still surfaced


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_resolution_failure_is_fail_soft(monkeypatch) -> None:
    async def boom(**kwargs):
        raise RuntimeError("kis down")

    monkeypatch.setattr(portfolio_holdings, "_collect_portfolio_positions", boom)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    assert out["count"] == 0
    assert out["results"] == []
    assert out["symbols_resolved"] == []
    assert "holdings_resolution_failed" in out["degraded_reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_concurrency_capped_at_semaphore(monkeypatch) -> None:
    import asyncio

    in_flight = 0
    peak = 0

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0.01)  # hold the slot so admitted coroutines overlap
            return _ok_result(symbol, market)
        finally:
            in_flight -= 1

    _patch_fetch(monkeypatch, fake_fetch)

    symbols = [f"{i:06d}" for i in range(20)]
    out = await _news._get_holdings_news_impl(symbols=symbols, limit_per_symbol=5)

    assert out["count"] == 20
    # the Semaphore admits at most HOLDINGS_NEWS_CONCURRENCY fetches at once
    # (>1 proves it is genuinely concurrent, <=N proves the cap holds).
    assert 1 < peak <= _news.HOLDINGS_NEWS_CONCURRENCY


@pytest.mark.unit
@pytest.mark.asyncio
async def test_holdings_partial_source_error_is_surfaced(monkeypatch) -> None:
    async def fake_collect(**kwargs):
        return (
            [{"symbol": "005930", "market": "kr", "name": "삼성전자"}],
            ["toss: timeout", "kis_us: 500"],  # two source errors
            None,
            None,
        )

    monkeypatch.setattr(
        portfolio_holdings, "_collect_portfolio_positions", fake_collect
    )

    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        return _ok_result(symbol, market)

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(symbols=None, limit_per_symbol=5)

    assert out["count"] == 1
    # a partial holdings-source failure is surfaced, not hidden (no-silent-drop)
    assert "holdings resolution partial" in out["degraded_reason"]
    assert "2 source error(s)" in out["degraded_reason"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_row_surfaces_unavailable_and_error_status(monkeypatch) -> None:
    async def fake_fetch(
        symbol, market, instrument_type=None, *, limit=20, timeout_s=5.0
    ):
        if symbol == "005930":
            # non-raising failure path: fetch_symbol_news returns status="unavailable"
            return SymbolNewsFetchResult(
                symbol,
                market,
                "naver",
                "unavailable",
                0,
                0,
                [],
                error_code="naver_fetch_failed",
            )
        # status="error" with no error_code -> falls back to "news_unavailable"
        return SymbolNewsFetchResult(symbol, market, "finnhub", "error", 0, 0, [])

    _patch_fetch(monkeypatch, fake_fetch)

    out = await _news._get_holdings_news_impl(
        symbols=["005930", "AAPL"], limit_per_symbol=5
    )

    by_symbol = {r["symbol"]: r for r in out["results"]}
    assert by_symbol["005930"]["status"] == "unavailable"
    assert by_symbol["005930"]["degraded_reason"] == "naver_fetch_failed"
    assert by_symbol["005930"]["news"] == []
    assert by_symbol["AAPL"]["status"] == "error"
    assert by_symbol["AAPL"]["degraded_reason"] == "news_unavailable"


@pytest.mark.unit
def test_get_holdings_news_registered_on_default_profile() -> None:
    from tests._mcp_tooling_support import build_tools

    tools = build_tools()
    assert "get_holdings_news" in tools
