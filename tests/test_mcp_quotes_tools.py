"""
Tests for MCP quotes/search/dividends tools.

This module contains tests for:
- search_symbol: Symbol search across markets (KR, US, crypto)
- get_quote: Real-time price quotes across markets
- get_dividends: Dividend information for US equities

These tests were extracted from tests/test_mcp_server_tools.py for better organization.
"""

import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from tests._mcp_tooling_support import (
    _patch_runtime_attr,
    _single_row_df,
    build_tools,
)

# ---------------------------------------------------------------------------
# search_symbol Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_symbol_empty_query_returns_empty():
    tools = build_tools()

    result = await tools["search_symbol"]("   ")

    assert result == []


@pytest.mark.asyncio
async def test_search_symbol_clamps_limit_and_shapes(monkeypatch):
    tools = build_tools()

    # Mock master data
    _patch_runtime_attr(
        monkeypatch,
        "search_kr_symbols",
        AsyncMock(
            return_value=[
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "instrument_type": "equity_kr",
                    "exchange": "KOSPI",
                    "is_active": True,
                },
                {
                    "symbol": "006400",
                    "name": "삼성SDI",
                    "instrument_type": "equity_kr",
                    "exchange": "KOSPI",
                    "is_active": True,
                },
            ]
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "search_us_symbols",
        AsyncMock(return_value=[]),
    )
    _patch_runtime_attr(
        monkeypatch,
        "search_upbit_symbols",
        AsyncMock(return_value=[]),
    )

    result = await tools["search_symbol"]("삼성", limit=500)

    # limit should be capped at 100
    assert len(result) == 2
    assert result[0]["symbol"] == "005930"
    assert result[0]["name"] == "삼성전자"
    assert result[0]["instrument_type"] == "equity_kr"
    assert result[0]["exchange"] == "KOSPI"


@pytest.mark.asyncio
async def test_search_symbol_with_market_filter(monkeypatch):
    tools = build_tools()

    # Mock master data
    _patch_runtime_attr(
        monkeypatch,
        "search_us_symbols",
        AsyncMock(
            return_value=[
                {
                    "symbol": "AAPL",
                    "name": "애플",
                    "instrument_type": "equity_us",
                    "exchange": "NASDAQ",
                    "is_active": True,
                }
            ]
        ),
    )

    # Search with us market filter
    result = await tools["search_symbol"]("애플", market="us")

    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_search_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()

    async def raise_error(*_args, **_kwargs):
        raise RuntimeError("master data failed")

    _patch_runtime_attr(monkeypatch, "search_kr_symbols", raise_error)

    result = await tools["search_symbol"]("samsung")

    assert len(result) == 1
    assert result[0]["error"] == "master data failed"
    assert result[0]["source"] == "master"
    assert result[0]["query"] == "samsung"


# ---------------------------------------------------------------------------
# get_quote Tests - Crypto
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_crypto(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(return_value={"KRW-BTC": 123.4})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

    result = await tools["get_quote"]("krw-btc")

    mock_fetch.assert_awaited_once_with(["KRW-BTC"])
    assert result == {
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "price": 123.4,
        "source": "upbit",
    }


@pytest.mark.asyncio
async def test_get_quote_crypto_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("upbit down"))
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

    result = await tools["get_quote"]("KRW-BTC")

    assert result == {
        "error": "upbit down",
        "source": "upbit",
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
    }


# ---------------------------------------------------------------------------
# get_quote Tests - Korean Equity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == pytest.approx(105.0)  # price = close
    assert result["open"] == pytest.approx(100.0)
    # ROB-448: single candle → previous_close is None (never 0, never raise)
    assert result["previous_close"] is None
    assert called["code"] == "005930"
    assert called["market"] == "J"
    assert called["n"] == 2  # ROB-448: 2 candles to surface previous_close


@pytest.mark.asyncio
async def test_get_quote_korean_equity_tags_premarket_data_state(monkeypatch):
    """ROB-464: KR get_quote tags data_state so a pre-market prior-close is not
    mistaken for a live price."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "premarket_unavailable",
    )

    result = await tools["get_quote"]("005930")

    assert result["instrument_type"] == "equity_kr"
    assert result["data_state"] == "premarket_unavailable"
    # The prior close is still surfaced as price (not dropped) — just flagged.
    assert result["price"] == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_get_quote_korean_equity_previous_close(monkeypatch):
    # ROB-448: with 2 candles, previous_close = the prior trading day's close.
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "open": 98.0,
                "high": 102.0,
                "low": 97.0,
                "close": 100.0,
                "volume": 900,
                "value": 90000.0,
            },
            {
                "date": "2024-01-02",
                "open": 100.0,
                "high": 110.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
            },
        ]
    )

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result["price"] == pytest.approx(105.0)  # latest close
    assert result["previous_close"] == pytest.approx(100.0)  # prior day's close


@pytest.mark.asyncio
async def test_get_quote_korean_equity_returns_error_payload(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            raise RuntimeError("kis down")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result == {
        "error": "kis down",
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_quote_korean_etf(monkeypatch):
    """Test get_quote with Korean ETF code (alphanumeric like 0123G0)."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0123G0")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == pytest.approx(105.0)


@pytest.mark.asyncio
async def test_get_quote_korean_etf_with_explicit_market(monkeypatch):
    """Test get_quote with Korean ETF code and explicit market=kr."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0117V0", market="kr")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol,label",
    [
        ("005930", "stock"),  # 삼성전자 — 일반 주식
        ("133690", "etf"),  # TIGER 은행TOP10 — ETF
    ],
)
async def test_fetch_quote_equity_kr_passes_market_j(monkeypatch, symbol, label):
    """Regression: market='J' is passed to KIS API for both stocks and ETFs (#487)."""
    tools = build_tools()
    df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"](symbol)

    assert called["market"] == "J", f"Expected market='J' for {label} symbol {symbol}"
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == pytest.approx(105.0)
    assert result["symbol"] == symbol


def _two_row_kr_quote_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "open": 98.0,
                "high": 102.0,
                "low": 97.0,
                "close": 100.0,
                "volume": 900,
                "value": 90000.0,
            },
            {
                "date": "2024-01-02",
                "open": 100.0,
                "high": 110.0,
                "low": 99.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
            },
        ]
    )


@pytest.fixture(autouse=True)
def _isolate_kr_nxt_tradability(monkeypatch):
    """Keep standalone quote tests independent of the shared NXT database."""
    from app.mcp_server.tooling import market_data_quotes

    async def no_tradability(symbols):
        return {}

    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", no_tradability)
    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", no_tradability
    )


def _nxt_quote_book(
    *,
    expected_price: int | None = None,
    asks: list[tuple[float, float]] | None = None,
    bids: list[tuple[float, float]] | None = None,
    empty: bool = False,
):
    import app.services.market_data as market_data_service

    return market_data_service.OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[
            market_data_service.OrderbookLevel(price=price, quantity=qty)
            for price, qty in (asks or [])
        ],
        bids=[
            market_data_service.OrderbookLevel(price=price, quantity=qty)
            for price, qty in (bids or [])
        ],
        total_ask_qty=0.0,
        total_bid_qty=0.0,
        bid_ask_ratio=None,
        expected_price=expected_price,
        expected_qty=None,
        venue="nxt",
        venue_label="NXT",
        kis_market_code="NX",
        source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        source_tr_id="FHKST01010200",
        is_empty_book=empty,
        requires_final_recheck=empty,
        empty_reason="empty_kis_orderbook" if empty else None,
    )


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_applies_in_premarket(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 173500.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_expected_price",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"symbol": "192820", "price": 168300.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price"] == 173500.0
    assert quote["price_source"] == "nxt_expected_price"
    assert quote["session"] == "nxt_premarket"
    assert quote["data_state"] == "fresh"
    assert quote["regular_session_data_state"] == "premarket_unavailable"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_surfaces_self_describing_fields(monkeypatch):
    """ROB-888: premarket overlay exposes krx_prev_close / change_pct /
    session_state so consumers judge the real gap without scraping CDP naver."""
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 2082500.0,  # NXT premarket realtime
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_mid",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    # Pre-overlay price is the most recent completed KRX regular close (prev day).
    quote = {"symbol": "000660", "price": 1913000.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "000660", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price"] == 2082500.0
    assert quote["price_source"] == "nxt_mid"
    assert quote["session_state"] == "premarket"
    assert quote["krx_prev_close"] == 1913000.0
    # (2082500 - 1913000) / 1913000 * 100 = 8.86%
    assert quote["change_pct"] == pytest.approx(8.86, abs=0.01)


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_after_hours_session_state(monkeypatch):
    """ROB-888: nxt_after overlay tags session_state=nxt_after; krx_prev_close is
    the just-closed KRX regular close."""
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_after"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 68500.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_mid",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        market_data_quotes,
        "_fetch_krx_regular_close",
        AsyncMock(return_value=(70000.0, "2026-11-26T15:30:00+09:00")),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "_fetch_quote_session_ohlcv",
        AsyncMock(
            return_value=({}, (70000.0, "2026-11-26T15:30:00+09:00"))
        ),
    )
    monkeypatch.setattr(
        market_data_quotes, "_fetch_venue_last_trade", AsyncMock(return_value=None)
    )

    import app.services.market_data as market_data_service

    krx_book = market_data_service.OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[market_data_service.OrderbookLevel(price=68510, quantity=4)],
        bids=[market_data_service.OrderbookLevel(price=68500, quantity=3)],
        total_ask_qty=4,
        total_bid_qty=3,
        bid_ask_ratio=0.75,
        as_of=dt.datetime(
            2026, 11, 26, 18, 16, tzinfo=dt.timezone(dt.timedelta(hours=9))
        ),
        venue="krx",
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=krx_book),
    )

    quote = {"symbol": "005930", "price": 70000.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="fresh", include_session_ohlcv=True
    )

    assert applied is True
    assert quote["session_state"] == "nxt_after"
    assert quote["krx_prev_close"] == 70000.0
    assert quote["krx_prev_close_source"] == "kis_krx_1530_closing_bar"
    assert quote["krx_prev_close_as_of"] == "2026-11-26T15:30:00+09:00"
    assert quote["venues"]["krx"]["best_bid"] == 68500.0
    assert quote["change_pct"] == pytest.approx(-2.14, abs=0.01)


@pytest.mark.asyncio
async def test_krx_after_venue_omits_pre_session_last_trade(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes as quotes
    from app.services import market_data as market_data_service
    from app.services.kr_quote_price import LastTrade

    snapshot = market_data_service.OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[],
        bids=[],
        total_ask_qty=0,
        total_bid_qty=0,
        bid_ask_ratio=0.0,
        as_of=dt.datetime(2026, 11, 26, 17, 0, tzinfo=quotes._KST),
        venue="krx",
    )
    monkeypatch.setattr(
        quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(
        quotes,
        "_fetch_venue_last_trade",
        AsyncMock(
            return_value=LastTrade(
                venue="krx",
                price=298500.0,
                traded_at=dt.datetime(2026, 11, 26, 15, 30, tzinfo=quotes._KST),
            )
        ),
    )

    venues = await quotes._fetch_krx_after_venue(
        "005930", now=dt.datetime(2026, 11, 26, 17, 0, tzinfo=quotes._KST)
    )

    assert "last_trade_price" not in venues["krx"]
    assert "last_trade_at" not in venues["krx"]


@pytest.mark.asyncio
async def test_nxt_quote_session_boundary_is_1540(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes as quotes

    monkeypatch.setattr(quotes, "get_kr_nxt_session_from_toss", AsyncMock(return_value=None))
    monkeypatch.setattr(quotes, "is_kr_session_day", lambda _day: True)
    day = dt.date(2026, 11, 26)

    before = await quotes._nxt_quote_session(
        "fresh", now=dt.datetime.combine(day, dt.time(15, 39), tzinfo=quotes._KST)
    )
    at_open = await quotes._nxt_quote_session(
        "fresh", now=dt.datetime.combine(day, dt.time(15, 40), tzinfo=quotes._KST)
    )
    after_open = await quotes._nxt_quote_session(
        "fresh", now=dt.datetime.combine(day, dt.time(15, 41), tzinfo=quotes._KST)
    )

    assert before is None
    assert at_open == "nxt_after"
    assert after_open == "nxt_after"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_change_pct_null_when_no_prev_close(monkeypatch):
    """ROB-888: no fake 0 / no raise when the pre-overlay KRX price is missing or
    non-positive — krx_prev_close and change_pct honestly report null."""
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 2082500.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_mid",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"symbol": "000660", "price": None, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "000660", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["session_state"] == "premarket"
    assert quote["krx_prev_close"] is None
    assert quote["change_pct"] is None


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_noop_outside_session(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return None

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)

    quote = {"symbol": "192820", "price": 168300.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="fresh"
    )

    assert applied is False
    assert quote == {"symbol": "192820", "price": 168300.0, "source": "kis"}


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_noop_on_empty_book(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return None  # empty orderbook → _fetch_nxt_quote_overlay returns None

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"symbol": "192820", "price": 168300.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "192820", quote, data_state="premarket_unavailable"
    )

    assert applied is False
    assert quote == {"symbol": "192820", "price": 168300.0}


@pytest.mark.asyncio
async def test_apply_nxt_after_empty_book_does_not_mutate_base_quote(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_after"

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes,
        "_fetch_nxt_quote_overlay",
        AsyncMock(return_value=None),
    )
    close = AsyncMock(return_value=(100.0, "2026-11-26T15:30:00+09:00"))
    bars = AsyncMock(return_value=({}, None))
    venue = AsyncMock(return_value={"krx": {"best_bid": 99.0}})
    monkeypatch.setattr(market_data_quotes, "_fetch_krx_regular_close", close)
    monkeypatch.setattr(market_data_quotes, "_fetch_quote_session_ohlcv", bars)
    monkeypatch.setattr(market_data_quotes, "_fetch_krx_after_venue", venue)

    quote = {"symbol": "005930", "price": 100.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="market_closed"
    )

    assert applied is False
    assert quote == {"symbol": "005930", "price": 100.0, "source": "kis"}
    close.assert_not_awaited()
    bars.assert_not_awaited()
    venue.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_session_bar_paging_respects_page_cap(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    class PagedKIS:
        calls = 0

        async def inquire_minute_chart(self, **kwargs):
            self.calls += 1
            base = dt.datetime(2026, 11, 26, 19, 0) - dt.timedelta(
                minutes=30 * (self.calls - 1)
            )
            return pd.DataFrame(
                [
                    {"datetime": base, "close": 100.0},
                    {"datetime": base - dt.timedelta(minutes=1), "close": 99.0},
                ]
            )

    kis = PagedKIS()
    now = dt.datetime(2026, 11, 26, 19, 0, tzinfo=market_data_quotes._KST)
    frame = await market_data_quotes._fetch_quote_venue_session_bars(
        kis,
        symbol="005930",
        market="J",
        now=now,
        session_open=dt.time(9, 0),
        page_cap=2,
    )

    assert kis.calls == 2
    assert len(frame) == 4


@pytest.mark.asyncio
async def test_internal_nxt_overlay_skips_session_ohlcv_by_default(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    async def fake_session(data_state, *, now=None):
        return "nxt_after"

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes,
        "_fetch_nxt_quote_overlay",
        AsyncMock(return_value={"price": 101.0, "venues": {"nxt": {}}}),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "_fetch_krx_regular_close",
        AsyncMock(return_value=(100.0, "2026-11-26T15:30:00+09:00")),
    )
    bars = AsyncMock(return_value=({}, None))
    monkeypatch.setattr(market_data_quotes, "_fetch_quote_session_ohlcv", bars)
    monkeypatch.setattr(
        market_data_quotes, "_fetch_krx_after_venue", AsyncMock(return_value={})
    )

    quote = {"symbol": "005930", "price": 100.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="market_closed"
    )

    assert applied is True
    assert quote["price"] == 101.0
    bars.assert_not_awaited()


@pytest.mark.asyncio
async def test_quote_session_ohlcv_reuses_krx_frame_for_regular_close(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes as quotes

    class FrameKIS:
        calls: list[str] = []

        async def inquire_minute_chart(self, *, market, **kwargs):
            self.calls.append(market)
            if market == "J":
                return pd.DataFrame(
                    [
                        {
                            "datetime": dt.datetime(2026, 11, 26, 9, 0),
                            "open": 290000,
                            "high": 291000,
                            "low": 289000,
                            "close": 290500,
                            "volume": 10,
                        },
                        {
                            "datetime": dt.datetime(2026, 11, 26, 15, 30),
                            "open": None,
                            "high": None,
                            "low": None,
                            "close": 298500,
                            "volume": 10623,
                        },
                    ]
                )
            return pd.DataFrame(
                [
                    {
                        "datetime": dt.datetime(2026, 11, 26, 16, 0),
                        "open": 299000,
                        "high": 300000,
                        "low": 298000,
                        "close": 299500,
                        "volume": 15,
                    }
                ]
            )

    kis = FrameKIS()
    monkeypatch.setattr(quotes, "KISClient", lambda: kis)

    session_ohlcv, krx_close = await quotes._fetch_quote_session_ohlcv(
        "278470", now=dt.datetime(2026, 11, 26, 17, 0, tzinfo=quotes._KST)
    )

    assert kis.calls == ["J", "NX"]
    assert krx_close == (298500.0, "2026-11-26T15:30:00+09:00")
    assert session_ohlcv["regular"]["close"] == 298500
    assert session_ohlcv["nxt_post"]["close"] == 299500


def _patch_nxt_eligible(monkeypatch, market_data_quotes) -> None:
    """hk 6300: premarket NXT overlay is gated on NXT tradability (now True)."""
    import datetime as dt

    from app.services.nxt_preflight import NxtTradability

    async def eligible(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=dt.datetime.now(dt.UTC),
            )
        }

    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", eligible)


@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_keeps_expected_out_of_price(
    monkeypatch,
):
    """hk 6498: NXT pre-market is continuous, so an expected-only book is no price.

    The expected price stays in its own field; the representative price is
    unavailable (no trade, no two-sided book) and the quote is not order basis.
    """
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            assert code == "005930"
            assert market == "J"
            assert n == 2
            return df

    get_orderbook_mock = AsyncMock(return_value=_nxt_quote_book(expected_price=114300))

    _patch_nxt_eligible(monkeypatch, market_data_quotes)
    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] is None
    assert result["expected_price"] == 114300
    assert result["price_kind"] == "unavailable"
    assert result["price_kind_reason"] == "no_trade_and_no_book"
    assert result["price_usable"] is False
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "premarket_unavailable"
    assert result["session"] == "nxt_premarket"
    assert result["price_session"] == "nxt_pre"
    assert result["venue"] == "nxt"
    assert result["venue_label"] == "NXT"
    assert result["kis_market_code"] == "NX"
    assert result["price_source"] is None


@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_routes_to_nxt_mid(
    monkeypatch,
):
    """ROB-511: use NXT best bid/ask mid when expected_price is absent."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(
        return_value=_nxt_quote_book(
            asks=[(114500, 10)],
            bids=[(114100, 20)],
        )
    )

    _patch_nxt_eligible(monkeypatch, market_data_quotes)
    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    assert result["price"] == pytest.approx(114300.0)
    assert result["price_source"] == "nxt_mid"
    assert result["session"] == "nxt_premarket"
    assert result["data_state"] == "fresh"


@pytest.mark.asyncio
async def test_get_quote_korean_equity_premarket_empty_nxt_book_keeps_stale_flag(
    monkeypatch,
):
    """ROB-511: empty NXT book keeps ROB-464 honest stale quote behavior."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import (
        DATA_STATE_PREMARKET_UNAVAILABLE,
    )

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_PREMARKET_UNAVAILABLE,
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=_nxt_quote_book(empty=True)),
    )

    result = await tools["get_quote"]("005930")

    assert result["price"] == pytest.approx(105.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "premarket_unavailable"
    assert "regular_session_data_state" not in result
    assert "session" not in result
    assert "venue" not in result
    assert "price_source" not in result


@pytest.mark.asyncio
async def test_get_quote_korean_equity_after_hours_routes_to_nxt(monkeypatch):
    """ROB-511: KR trading-day NXT after-hours quote also uses NXT evidence."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(return_value=_nxt_quote_book(expected_price=113900))
    session_bars = AsyncMock(return_value=({}, None))

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "market_closed",
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 17:00:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )
    monkeypatch.setattr(
        market_data_quotes, "_fetch_quote_session_ohlcv", session_bars
    )

    result = await tools["get_quote"]("005930")

    assert get_orderbook_mock.await_args_list == [
        (("005930", "kr"), {"venue": "nxt"}),
        (("005930", "kr"), {"venue": "krx"}),
    ]
    # hk 6498: NXT post is continuous; an expected-only book is not the price.
    assert result["price"] is None
    assert result["expected_price"] == 113900
    assert result["price_kind"] == "unavailable"
    assert result["price_session"] == "nxt_post"
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "market_closed"
    assert result["session"] == "nxt_after"
    assert result["venue"] == "nxt"
    assert result["price_source"] is None
    session_bars.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_quote_korean_equity_does_not_route_to_nxt_at_1535(monkeypatch):
    """15:30-16:00 is outside both after-market quote sessions."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock(return_value=_nxt_quote_book(expected_price=113900))

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "market_closed",
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 15:35:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "get_kr_nxt_session_from_toss",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_not_awaited()
    assert "session" not in result


@pytest.mark.asyncio
async def test_get_quote_korean_equity_respects_toss_partial_nxt_holiday(monkeypatch):
    """ROB-536: Toss calendar can close NXT after while XKRX regular day exists."""
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock()

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: "market_closed",
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 15:45:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "get_kr_nxt_session_from_toss",
        AsyncMock(return_value="closed"),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_not_awaited()
    assert result["data_state"] == "market_closed"
    assert "session" not in result


@pytest.mark.asyncio
async def test_get_quote_korean_equity_regular_session_skips_nxt_orderbook(
    monkeypatch,
):
    """ROB-511: regular KRX session keeps the existing daily quote path."""
    from app.mcp_server.tooling import market_data_quotes
    from app.mcp_server.tooling.market_session import DATA_STATE_FRESH

    tools = build_tools()
    df = _two_row_kr_quote_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    get_orderbook_mock = AsyncMock()

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        market_data_quotes,
        "kr_market_data_state",
        lambda *a, **k: DATA_STATE_FRESH,
    )
    monkeypatch.setattr(
        market_data_quotes,
        "now_kst",
        lambda: pd.Timestamp("2026-06-11 10:00:00", tz="Asia/Seoul").to_pydatetime(),
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        get_orderbook_mock,
    )

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_not_awaited()
    assert result["price"] == pytest.approx(105.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "fresh"
    assert (result["open"], result["high"], result["low"], result["volume"]) == (
        100.0,
        110.0,
        99.0,
        1000,
    )
    assert "session_ohlcv" not in result
    assert "krx_prev_close" not in result
    assert "regular_session_data_state" not in result
    assert "session" not in result


# ---------------------------------------------------------------------------
# get_quote Tests - US Equity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    """KIS-primary happy path: source=kis_overseas, Yahoo 미호출, resolved exchange 전달."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NYSE")
    )
    price_df = pd.DataFrame(
        [{"close": 205.0, "previous_close": 201.5, "volume": 123456789}]
    )
    captured: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            captured["symbol"] = symbol
            captured["exchange_code"] = exchange_code
            return price_df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=AssertionError("Yahoo should not be called")),
    )

    result = await tools["get_quote"]("AAPL")

    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "kis_overseas"
    assert result["price"] == pytest.approx(205.0)
    assert result["previous_close"] == pytest.approx(201.5)
    assert result["volume"] == 123456789
    assert result["open"] is None
    assert result["high"] is None
    assert result["low"] is None
    assert result["delayed"] is True
    # ROB-471: the DB-resolved exchange + symbol are threaded into the KIS call
    # (regression guard — a dropped exchange_code arg would default to NASD).
    assert captured["exchange_code"] == "NYSE"
    assert captured["symbol"] == "AAPL"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc_name", ["USSymbolInactiveError", "USSymbolUniverseEmptyError"]
)
async def test_get_quote_us_lookup_exc_then_yahoo_no_price_is_unavailable(
    monkeypatch, exc_name
):
    """fast_info succeeds but has no price -> quote_unavailable, not symbol_not_found."""
    import app.services.us_symbol_universe_service as uss

    exc = getattr(uss, exc_name)
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=exc("no route")),
    )
    monkeypatch.setattr(
        yahoo_service, "fetch_fast_info", AsyncMock(return_value={"close": None})
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_kis_raises_then_yahoo_succeeds(monkeypatch):
    """KIS infra error + Yahoo 정상가격 → fallback 성공(source=yahoo, no raise)."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            raise RuntimeError("kis http 500")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(
            return_value={
                "close": 205.0,
                "previous_close": 201.5,
                "open": 202.0,
                "high": 206.2,
                "low": 200.8,
                "volume": 123456789,
            }
        ),
    )

    result = await tools["get_quote"]("AAPL")

    assert result["source"] == "yahoo"
    assert result["price"] == pytest.approx(205.0)
    assert result["delayed"] is True


@pytest.mark.asyncio
async def test_get_quote_us_falls_back_to_yahoo(monkeypatch):
    """KIS empty → Yahoo fallback, source=yahoo."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(columns=["close", "previous_close", "volume"])

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    mock_fast_info = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "close": 205.0,
            "previous_close": 201.5,
            "open": 202.0,
            "high": 206.2,
            "low": 200.8,
            "volume": 123456789,
        }
    )
    monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fast_info)

    result = await tools["get_quote"]("AAPL")

    assert result["source"] == "yahoo"
    assert result["price"] == pytest.approx(205.0)
    assert result["open"] == pytest.approx(202.0)
    assert result["delayed"] is True
    mock_fast_info.assert_awaited_once_with("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_symbol_not_found(monkeypatch):
    """Yahoo explicit not-found errors stay symbol_not_found."""
    from app.services.us_symbol_universe_service import USSymbolNotRegisteredError

    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=USSymbolNotRegisteredError("not registered")),
    )
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=ValueError("Quote not found for symbol: INVALID")),
    )

    with pytest.raises(ValueError, match="Symbol 'INVALID' not found"):
        await tools["get_quote"]("INVALID")


@pytest.mark.asyncio
async def test_get_quote_us_quote_unavailable(monkeypatch):
    """KIS infra error + Yahoo close=None → RuntimeError quote_unavailable (not 'not found')."""
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            raise RuntimeError("kis http 500")

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service, "fetch_fast_info", AsyncMock(return_value={"close": None})
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_equity_propagates_upstream_exception(monkeypatch):
    """KIS no-route + Yahoo transport 실패 → RuntimeError (원인 메시지 보존)."""
    from app.services.us_symbol_universe_service import USSymbolNotRegisteredError

    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=USSymbolNotRegisteredError("not registered")),
    )
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=RuntimeError("yahoo down")),
    )

    with pytest.raises(RuntimeError, match="yahoo down"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_flag_off_uses_yahoo(monkeypatch):
    """us_quote_kis_primary=False → KIS 경로 스킵, Yahoo primary."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "us_quote_kis_primary", False)
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch,
        "get_us_exchange_by_symbol",
        AsyncMock(side_effect=AssertionError("KIS path should be skipped")),
    )
    mock_fast_info = AsyncMock(
        return_value={
            "symbol": "AAPL",
            "close": 205.0,
            "previous_close": 201.5,
            "open": 202.0,
            "high": 206.2,
            "low": 200.8,
            "volume": 123456789,
        }
    )
    monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fast_info)

    result = await tools["get_quote"]("AAPL")

    assert result["source"] == "yahoo"
    assert result["price"] == pytest.approx(205.0)


@pytest.mark.asyncio
async def test_get_quote_us_flag_off_yahoo_no_price_is_unavailable(monkeypatch):
    """Yahoo primary fast_info with no price is a runtime data gap, not not-found."""
    from app.core.config import settings as app_settings

    monkeypatch.setattr(app_settings, "us_quote_kis_primary", False)
    tools = build_tools()
    monkeypatch.setattr(
        yahoo_service, "fetch_fast_info", AsyncMock(return_value={"close": None})
    )

    with pytest.raises(RuntimeError, match="temporarily unavailable"):
        await tools["get_quote"]("AAPL")


@pytest.mark.asyncio
async def test_get_quote_us_kis_tags_premarket_session_and_quote_asof(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "premarket")

    price_df = pd.DataFrame(
        [
            {
                "close": 195.29,
                "previous_close": 194.83,
                "volume": 123456,
                "quote_asof": "2026-07-06T08:45:12-04:00",
            }
        ]
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return price_df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(side_effect=AssertionError("Yahoo should not be called")),
    )

    result = await tools["get_quote"]("NVDA", market="us")

    assert result["source"] == "kis_overseas"
    assert result["price"] == pytest.approx(195.29)
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["quote_asof"] == "2026-07-06T08:45:12-04:00"
    assert result["price_source"] == "kis_overseas_last"
    assert result["venue"] == "NASD"
    assert result["delayed"] is True


@pytest.mark.asyncio
async def test_get_quote_us_closed_session_is_stale(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NYSE")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "closed")

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(
                [{"close": 205.0, "previous_close": 201.5, "volume": 100}]
            )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("AAPL", market="us")

    assert result["session"] == "closed"
    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "us_market_closed"
    assert result["price_source"] == "kis_overseas_last"


@pytest.mark.asyncio
async def test_get_quote_us_yahoo_fallback_tags_session_and_price_source(monkeypatch):
    tools = build_tools()

    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: "regular")

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return pd.DataFrame(
                columns=["close", "previous_close", "volume", "quote_asof"]
            )

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_fast_info",
        AsyncMock(
            return_value={
                "close": 205.0,
                "previous_close": 201.5,
                "open": 202.0,
                "high": 206.2,
                "low": 200.8,
                "volume": 123456789,
            }
        ),
    )

    result = await tools["get_quote"]("AAPL", market="us")

    assert result["source"] == "yahoo"
    assert result["session"] == "regular"
    assert result["data_state"] == "fresh"
    assert result["price_source"] == "yahoo_fast_info_close"
    assert "quote_asof" not in result


# ---------------------------------------------------------------------------
# get_quote Tests - US Equity include_extended_hours (ROB-922)
# ---------------------------------------------------------------------------


def _setup_us_kis_premarket_quote(monkeypatch, session: str) -> None:
    """Shared fixture: KIS overseas quote in the given (non-regular) session."""
    _patch_runtime_attr(
        monkeypatch, "get_us_exchange_by_symbol", AsyncMock(return_value="NASD")
    )
    _patch_runtime_attr(monkeypatch, "us_market_session", lambda *a, **k: session)

    price_df = pd.DataFrame(
        [
            {
                "close": 195.29,
                "previous_close": 194.83,
                "volume": 123456,
                "quote_asof": "2026-07-06T08:45:12-04:00",
            }
        ]
    )

    class DummyKISClient:
        async def inquire_overseas_price(self, symbol, exchange_code="NASD"):
            return price_df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_default_false_is_noop(monkeypatch):
    """Unspecified include_extended_hours must never call the Yahoo prepost
    path — default result stays byte-identical to the pre-ROB-922 payload."""
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "premarket")
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=AssertionError("prepost must not be called")),
    )

    result = await tools["get_quote"]("NVDA", market="us")

    assert result["price"] == pytest.approx(195.29)
    assert result["price_source"] == "kis_overseas_last"
    assert result["session"] == "premarket"


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_premarket_overlays_yahoo_prepost(
    monkeypatch,
):
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "premarket")
    # hk 6521: a 20-second-old bar is fresh; the bar time is relative to now.
    bar_asof = (dt.datetime.now(dt.UTC) - dt.timedelta(seconds=20)).isoformat()
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(
            return_value={
                "symbol": "NVDA",
                "price": 210.5,
                "quote_asof": bar_asof,
                "volume": 5000,
            }
        ),
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["price"] == pytest.approx(210.5)
    assert result["price_source"] == "yahoo_prepost_last"
    assert result["quote_asof"] == bar_asof
    assert result["session"] == "premarket"
    assert result["data_state"] == "fresh"
    assert result["is_stale_price"] is False
    assert "data_state_reason" not in result
    assert result["quote_age_seconds"] in (19, 20, 21)
    assert result["received_at"]
    # previous_close must be preserved from the base source for gap math.
    assert result["previous_close"] == pytest.approx(194.83)


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_afterhours_overlays_yahoo_prepost(
    monkeypatch,
):
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "afterhours")
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(
            return_value={
                "symbol": "NVDA",
                "price": 208.1,
                "quote_asof": "2026-07-16T21:15:00+00:00",
                "volume": 900,
            }
        ),
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["price"] == pytest.approx(208.1)
    assert result["price_source"] == "yahoo_prepost_last"
    assert result["session"] == "afterhours"


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_prepost_none_keeps_honest_label(
    monkeypatch,
):
    """When Yahoo has no prepost data, the existing (honest) quote/labels are
    kept — never lie about the price_source."""
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "premarket")
    monkeypatch.setattr(
        yahoo_service, "fetch_prepost_quote", AsyncMock(return_value=None)
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["price"] == pytest.approx(195.29)
    assert result["price_source"] == "kis_overseas_last"
    assert result["session"] == "premarket"


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_prepost_exception_keeps_honest_label(
    monkeypatch,
):
    """A Yahoo transport failure during the opt-in overlay must not break the
    base quote — degrade gracefully to the existing result."""
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "premarket")
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=RuntimeError("yahoo down")),
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["price"] == pytest.approx(195.29)
    assert result["price_source"] == "kis_overseas_last"


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_regular_session_is_noop(
    monkeypatch,
):
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "regular")
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=AssertionError("prepost must not be called")),
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["price"] == pytest.approx(195.29)
    assert result["price_source"] == "kis_overseas_last"
    assert result["session"] == "regular"


@pytest.mark.asyncio
async def test_get_quote_us_include_extended_hours_closed_session_is_noop(
    monkeypatch,
):
    tools = build_tools()
    _setup_us_kis_premarket_quote(monkeypatch, "closed")
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=AssertionError("prepost must not be called")),
    )

    result = await tools["get_quote"]("NVDA", market="us", include_extended_hours=True)

    assert result["session"] == "closed"
    assert result["data_state"] == "stale"
    assert result["price_source"] == "kis_overseas_last"


# ---------------------------------------------------------------------------
# hk 6521: overlay freshness is decided from the bar age, not assumed fresh.
# ---------------------------------------------------------------------------

_REQUEST_AT = dt.datetime(2026, 11, 25, 10, 0, 0, tzinfo=dt.UTC)


def _us_premarket_base_quote() -> dict:
    return {
        "symbol": "NVDA",
        "instrument_type": "equity_us",
        "price": 195.29,
        "previous_close": 194.83,
        "volume": 123456,
        "source": "kis_overseas",
        "delayed": True,
        "venue": "NASD",
        "price_source": "kis_overseas_last",
        "session": "premarket",
        "data_state": "fresh",
        "quote_asof": "2026-11-25T08:45:12-04:00",
    }


def _prepost_payload(quote_asof):
    return {"symbol": "NVDA", "price": 210.5, "quote_asof": quote_asof, "volume": 5000}


@pytest.mark.asyncio
async def test_extended_overlay_bar_32_minutes_old_is_stale_with_age_reason(
    monkeypatch,
):
    from app.mcp_server.tooling import market_data_quotes

    old = (_REQUEST_AT - dt.timedelta(minutes=32)).isoformat()
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(return_value=_prepost_payload(old)),
    )

    result = await market_data_quotes._apply_extended_hours_overlay(
        _us_premarket_base_quote(),
        "NVDA",
        True,
        clock=lambda: _REQUEST_AT,
    )

    assert result["price"] == pytest.approx(210.5)
    assert result["price_source"] == "yahoo_prepost_last"
    assert result["data_state"] == "stale"
    assert result["data_state_reason"] == "extended_quote_age_1920s_exceeds_300s"
    assert result["is_stale_price"] is True
    assert result["quote_asof"] == old
    assert result["quote_age_seconds"] == 1920
    assert result["received_at"] == _REQUEST_AT.isoformat()
    assert result["delayed"] is True


@pytest.mark.asyncio
async def test_extended_overlay_bar_20_seconds_old_is_fresh(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    recent = (_REQUEST_AT - dt.timedelta(seconds=20)).isoformat()
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(return_value=_prepost_payload(recent)),
    )

    result = await market_data_quotes._apply_extended_hours_overlay(
        _us_premarket_base_quote(),
        "NVDA",
        True,
        clock=lambda: _REQUEST_AT,
    )

    assert result["data_state"] == "fresh"
    assert "data_state_reason" not in result
    assert result["is_stale_price"] is False
    assert result["quote_asof"] == recent
    assert result["quote_age_seconds"] == 20
    assert result["received_at"] == _REQUEST_AT.isoformat()


@pytest.mark.asyncio
async def test_extended_overlay_missing_quote_asof_is_unknown(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(return_value=_prepost_payload(None)),
    )

    result = await market_data_quotes._apply_extended_hours_overlay(
        _us_premarket_base_quote(),
        "NVDA",
        True,
        clock=lambda: _REQUEST_AT,
    )

    assert result["data_state"] == "unknown"
    assert result["data_state_reason"] == "quote_asof_missing"
    assert result["is_stale_price"] is True
    # The base quote's KIS timestamp must not be attached to the prepost price.
    assert result["quote_asof"] is None
    assert result["quote_age_seconds"] is None
    assert result["price"] == pytest.approx(210.5)


@pytest.mark.asyncio
async def test_extended_overlay_unparsable_quote_asof_is_unknown(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(return_value=_prepost_payload("not-a-timestamp")),
    )

    result = await market_data_quotes._apply_extended_hours_overlay(
        _us_premarket_base_quote(),
        "NVDA",
        True,
        clock=lambda: _REQUEST_AT,
    )

    assert result["data_state"] == "unknown"
    assert result["data_state_reason"] == "quote_asof_unparsable"
    assert result["quote_age_seconds"] is None


@pytest.mark.asyncio
async def test_extended_overlay_never_copies_bar_volume_into_quote_volume(
    monkeypatch,
):
    from app.mcp_server.tooling import market_data_quotes

    recent = (_REQUEST_AT - dt.timedelta(seconds=5)).isoformat()
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(return_value=_prepost_payload(recent)),
    )

    result = await market_data_quotes._apply_extended_hours_overlay(
        _us_premarket_base_quote(),
        "NVDA",
        True,
        clock=lambda: _REQUEST_AT,
    )

    # The 1-minute prepost bar volume (5000) is not the quote volume (123456).
    assert result["volume"] == 123456


@pytest.mark.asyncio
async def test_get_quote_kr_include_extended_hours_is_noop(monkeypatch):
    """KR market ignores include_extended_hours entirely — no-op."""
    tools = build_tools()

    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=AssertionError("prepost must not be called for KR")),
    )

    result = await tools["get_quote"]("005930", include_extended_hours=True)

    assert result["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_get_quote_crypto_include_extended_hours_is_noop(monkeypatch):
    """Crypto market ignores include_extended_hours entirely — no-op."""
    tools = build_tools()

    mock_fetch = AsyncMock(return_value={"KRW-BTC": 123.4})
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)
    monkeypatch.setattr(
        yahoo_service,
        "fetch_prepost_quote",
        AsyncMock(side_effect=AssertionError("prepost must not be called for crypto")),
    )

    result = await tools["get_quote"]("krw-btc", include_extended_hours=True)

    assert result["instrument_type"] == "crypto"


# ---------------------------------------------------------------------------
# get_quote Tests - Error Handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_non_us_markets_keep_error_payload_contract(monkeypatch):
    tools = build_tools()

    mock_fetch = AsyncMock(side_effect=RuntimeError("upbit down"))
    monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

    result = await tools["get_quote"]("KRW-BTC")

    assert result == {
        "error": "upbit down",
        "source": "upbit",
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
    }


@pytest.mark.asyncio
async def test_get_quote_raises_on_invalid_symbol():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_quote"]("")

    # Note: Numeric symbols like "1234" are now normalized to "001234" for KR market,
    # so we test with a clearly invalid format instead
    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("!@#$")


# ---------------------------------------------------------------------------
# get_quote Tests - Market Parameter Validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_quote_market_crypto_requires_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="crypto symbols must include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("BTC", market="crypto")


@pytest.mark.asyncio
async def test_get_quote_market_kr_requires_digits():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_quote"]("AAPL", market="kr")


@pytest.mark.asyncio
async def test_get_quote_market_us_rejects_crypto_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="us equity symbols must not include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("KRW-BTC", market="us")


# ---------------------------------------------------------------------------
# get_dividends Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_dividends_uses_session_and_keeps_payload(monkeypatch):
    tools = build_tools()
    captured: dict[str, object] = {}

    class MockTicker:
        info = {
            "dividendYield": 0.01234,
            "dividendRate": 1.11,
            "exDividendDate": 1704067200,
        }
        dividends = pd.Series(
            [1.0, 1.2],
            index=pd.to_datetime(["2024-01-01", "2024-04-01"]),
        )

    def ticker_factory(symbol, session=None):
        captured["symbol"] = symbol
        captured["session"] = session
        return MockTicker()

    monkeypatch.setattr("yfinance.Ticker", ticker_factory)

    result = await tools["get_dividends"]("aapl")

    assert result["success"] is True
    assert result["symbol"] == "AAPL"
    assert result["dividend_yield"] == pytest.approx(0.0123)
    assert result["dividend_rate"] == pytest.approx(1.11)
    assert result["ex_dividend_date"] == "2024-01-01"
    assert result["last_dividend"] == pytest.approx(
        {"date": "2024-04-01", "amount": 1.2}
    )
    assert captured["symbol"] == "AAPL"
    assert captured["session"] is not None


@pytest.mark.asyncio
async def test_get_quote_kr_exposes_nxt_tradable(monkeypatch):
    import datetime as dt

    from app.mcp_server.tooling import market_data_quotes
    from app.services.nxt_preflight import NxtTradability

    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    async def fake_tradability(symbols, db=None):
        return {
            symbols[0]: NxtTradability(
                nxt_eligible=True,
                nxt_trading_suspended=False,
                asof=dt.datetime(2026, 7, 3, 6, 0, tzinfo=dt.UTC),
            )
        }

    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", fake_tradability)

    result = await tools["get_quote"]("005930")
    assert result["nxt_tradable"] is None
    assert result["nxt_tradable_observed"] is True
    assert result["nxt_tradable_stale"] is True
    assert result["nxt_tradable_reason"] == "stale_asof"
    assert result["nxt_tradable_source"] == "kr_symbol_universe"
    assert result["nxt_tradable_asof"] is not None


def _krx_after_quote_setup(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    return tools, market_data_quotes


def _krx_after_capability(**overrides):
    import datetime as dt

    from app.services.krx_after_market import KrxAfterTradability

    fields = {
        "listed": True,
        "exchange": "KOSPI",
        "security_type": "STOCK",
        "krx_trading_suspended": False,
        "asof": dt.datetime.now(dt.UTC) - dt.timedelta(hours=1),
        "list_source": "krx-notice-test",
    }
    fields.update(overrides)
    return KrxAfterTradability(**fields)


def test_kr_quote_after_fixture_uses_krx_close_and_splits_sessions(monkeypatch):
    """Replay the supplied 278470 evidence, including the 15:30 close bar."""
    from app.mcp_server.tooling import market_data_quotes as quotes
    from app.services.research_candles import session_segment

    monkeypatch.setattr(session_segment, "is_trading_session", lambda *_: True)
    session_segment._kr_session_day.cache_clear()
    fixture_date = dt.date(2026, 11, 26)
    krx_fixture = pd.DataFrame(
        [
            {
                "datetime": dt.datetime(2026, 11, 26, 15, 30),
                "open": None,
                "high": None,
                "low": None,
                "close": 298500,
                "volume": 10623,
            },
            {
                "datetime": dt.datetime(2026, 11, 26, 17, 8),
                "open": None,
                "high": 343000,
                "low": None,
                "close": None,
                "volume": None,
            },
        ]
    )
    nxt_fixture = pd.DataFrame(
        [
            {
                "datetime": dt.datetime(2026, 11, 26, 16, 1),
                "open": 328000,
                "high": 330000,
                "low": 327000,
                "close": 329000,
                "volume": 25,
            }
        ]
    )

    assert quotes._krx_regular_close_from_frame(
        krx_fixture, session_date=fixture_date
    ) == (298500.0, "2026-11-26T15:30:00+09:00")
    session_ohlcv = quotes._quote_session_ohlcv_from_frame(krx_fixture, venue="krx")
    session_ohlcv.update(
        quotes._quote_session_ohlcv_from_frame(nxt_fixture, venue="nxt")
    )
    assert session_ohlcv["regular"]["close"] == 298500
    assert session_ohlcv["regular"]["volume"] == 10623
    assert session_ohlcv["krx_after"]["high"] == 343000
    assert session_ohlcv["nxt_post"]["close"] == 329000


@pytest.mark.asyncio
async def test_get_quote_kr_exposes_krx_after_tradable(monkeypatch):
    tools, market_data_quotes = _krx_after_quote_setup(monkeypatch)

    async def fake_krx(symbols, db=None):
        return {symbols[0]: _krx_after_capability()}

    monkeypatch.setattr(market_data_quotes, "get_kr_krx_after_tradability", fake_krx)

    result = await tools["get_quote"]("375500")
    assert result["krx_after_tradable"] is True
    assert result["krx_after_tradable_source"] == "krx_after_market_eligibility"
    assert result["krx_after_tradable_list_source"] == "krx-notice-test"
    assert result["krx_after_tradable_stale"] is False
    assert result["krx_after_tradable_reason"] == "krx_after_tradable"
    assert result["krx_after_tradable_asof"] is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"security_type": "ETF"}, "etf_etn_excluded"),
        ({"security_type": "ETN"}, "etf_etn_excluded"),
        ({"listed": False}, "not_krx_after_listed"),
        ({"listed": None, "asof": None}, "missing_asof"),
        ({"security_type": None}, "security_type_unknown"),
    ],
)
async def test_get_quote_krx_after_false_without_positive_evidence(
    monkeypatch, overrides, reason
):
    tools, market_data_quotes = _krx_after_quote_setup(monkeypatch)

    async def fake_krx(symbols, db=None):
        return {symbols[0]: _krx_after_capability(**overrides)}

    monkeypatch.setattr(market_data_quotes, "get_kr_krx_after_tradability", fake_krx)

    result = await tools["get_quote"]("459580")
    assert result["krx_after_tradable"] is False
    assert result["krx_after_tradable_reason"] == reason


@pytest.mark.asyncio
async def test_get_quote_krx_after_unknown_symbol_or_lookup_failure_is_false(
    monkeypatch,
):
    tools, market_data_quotes = _krx_after_quote_setup(monkeypatch)

    async def missing(symbols, db=None):
        return {}

    monkeypatch.setattr(market_data_quotes, "get_kr_krx_after_tradability", missing)
    result = await tools["get_quote"]("375500")
    assert result["krx_after_tradable"] is False
    assert result["krx_after_tradable_reason"] == "not_in_active_universe"
    assert "error" not in result

    async def schema_fault(symbols, db=None):
        raise RuntimeError('relation "krx_after_market_eligibility" does not exist')

    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", schema_fault
    )
    result = await tools["get_quote"]("375500")
    assert result["krx_after_tradable"] is False
    assert result["krx_after_tradable_reason"] == "lookup_failed"
    assert result["krx_after_tradable_stale"] is True
    assert "error" not in result


@pytest.mark.asyncio
async def test_registered_get_quote_surfaces_nxt_schema_fault(monkeypatch):
    from app.mcp_server.tooling import market_data_quotes

    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    async def schema_fault(symbols):
        raise RuntimeError('relation "kr_symbol_universe" does not exist')

    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", schema_fault)

    result = await tools["get_quote"]("005930", market="kr")

    assert result == {
        "error": 'relation "kr_symbol_universe" does not exist',
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_quote_us_has_no_nxt_fields(monkeypatch):
    tools = build_tools()

    async def fake_fast_info(_symbol):
        return {"close": 100.0, "previous_close": 99.0}

    monkeypatch.setattr(yahoo_service, "fetch_fast_info", fake_fast_info)
    result = await tools["get_quote"]("AAPL", "us")
    assert "nxt_tradable" not in result
