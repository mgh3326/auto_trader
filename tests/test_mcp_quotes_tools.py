"""
Tests for MCP quotes/search/dividends tools.

This module contains tests for:
- search_symbol: Symbol search across markets (KR, US, crypto)
- get_quote: Real-time price quotes across markets
- get_dividends: Dividend information for US equities

These tests were extracted from tests/test_mcp_server_tools.py for better organization.
"""

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
    from app.mcp_server.tooling import name_resolution

    monkeypatch.setattr(
        name_resolution,
        "get_upbit_market_display_names",
        AsyncMock(return_value={}),
    )

    result = await tools["get_quote"]("krw-btc")

    mock_fetch.assert_awaited_once_with(["KRW-BTC"])
    assert result == {
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "price": 123.4,
        "source": "upbit",
        "name": "KRW-BTC",
        "name_resolved": False,
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
async def test_get_quote_korean_equity_premarket_routes_to_nxt_expected_price(
    monkeypatch,
):
    """ROB-511: pre-market KR quote uses NXT expected price when available."""
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
    assert result["price"] == pytest.approx(114300.0)
    assert result["previous_close"] == pytest.approx(100.0)
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "premarket_unavailable"
    assert result["session"] == "nxt_premarket"
    assert result["venue"] == "nxt"
    assert result["venue_label"] == "NXT"
    assert result["kis_market_code"] == "NX"
    assert result["price_source"] == "nxt_expected_price"


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

    result = await tools["get_quote"]("005930")

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["price"] == pytest.approx(113900.0)
    assert result["data_state"] == "fresh"
    assert result["regular_session_data_state"] == "market_closed"
    assert result["session"] == "nxt_after"
    assert result["venue"] == "nxt"
    assert result["price_source"] == "nxt_expected_price"


@pytest.mark.asyncio
async def test_get_quote_korean_equity_after_hours_routes_to_nxt_at_1535(monkeypatch):
    """ROB-536: NXT after starts at 15:30 KST, not 16:00."""
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

    get_orderbook_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["session"] == "nxt_after"
    assert result["data_state"] == "fresh"


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
