"""hk 6300: KR get_quote before the KRX open (premarket_unavailable).

Before 09:00 KST the KIS daily row for today is the prior session carried over
(open/high/low/close = prior close, volume 0, date-only price_as_of). These tests
pin the honest contract: carried KRX OHLC is marked, a zero volume is never a trade
count, NXT no-trade is explicit, date-only is never fresh, and a symbol that NXT
does not report as tradable is not usable before the open. Regular-session output
keeps its previous shape.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes
from app.mcp_server.tooling.market_session import (
    DATA_STATE_PREMARKET_UNAVAILABLE,
)
from app.services.market_data.contracts import OrderbookLevel, OrderbookSnapshot
from app.services.nxt_preflight import NxtTradability
from tests._mcp_tooling_support import build_tools

KST = ZoneInfo("Asia/Seoul")
NOW_PREMARKET = dt.datetime(2026, 11, 25, 8, 2, 0, tzinfo=KST)


def _carried_daily_frame() -> pd.DataFrame:
    """Prior session 11-24 close 99; today's carried row 11-25 = prior close."""
    return pd.DataFrame(
        [
            {
                "date": "2026-11-23",
                "open": 97.0,
                "high": 98.0,
                "low": 96.0,
                "close": 98.0,
                "volume": 800,
                "value": 78400.0,
            },
            {
                "date": "2026-11-24",
                "open": 98.0,
                "high": 100.0,
                "low": 97.0,
                "close": 99.0,
                "volume": 900,
                "value": 89100.0,
            },
            {
                "date": "2026-11-25",
                "open": 99.0,
                "high": 99.0,
                "low": 99.0,
                "close": 99.0,
                "volume": 0,
                "value": 0.0,
            },
        ]
    )


def _nxt_book(*, asks, bids, as_of, empty=False) -> OrderbookSnapshot:
    return OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=[OrderbookLevel(price=p, quantity=q) for p, q in asks],
        bids=[OrderbookLevel(price=p, quantity=q) for p, q in bids],
        total_ask_qty=100,
        total_bid_qty=100,
        bid_ask_ratio=1.0,
        as_of=as_of,
        price_as_of_source="broker",
        venue="nxt",
        venue_label="NXT",
        kis_market_code="NX",
        is_empty_book=empty,
    )


def _setup(
    monkeypatch,
    *,
    tradability: NxtTradability | None,
    orderbook: OrderbookSnapshot | None = None,
    state: str = DATA_STATE_PREMARKET_UNAVAILABLE,
):
    class DummyKIS:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return _carried_daily_frame()

    async def fake_tradability(symbols, db=None):
        return {symbols[0]: tradability} if tradability is not None else {}

    async def fake_krx_after(symbols, db=None):
        return {}

    async def fake_session(data_state, *, now=None):
        return "nxt_premarket" if state == DATA_STATE_PREMARKET_UNAVAILABLE else None

    book_mock = AsyncMock(return_value=orderbook)
    monkeypatch.setattr(market_data_quotes, "KISClient", DummyKIS)
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: NOW_PREMARKET)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: state
    )
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", fake_tradability)
    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", fake_krx_after
    )
    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(
        market_data_quotes.market_data_service, "get_orderbook", book_mock
    )
    return book_mock


def _eligible() -> NxtTradability:
    # public_fields() judges staleness against the real clock, so use real now.
    return NxtTradability(
        nxt_eligible=True,
        nxt_trading_suspended=False,
        asof=dt.datetime.now(KST),
    )


# --- Acceptance 1: NXT-eligible symbol at 08:01 with an NXT mid ----------------


@pytest.mark.asyncio
async def test_premarket_nxt_mid_marks_krx_carried_and_no_trade(monkeypatch):
    book = _nxt_book(
        asks=[(100.5, 10)],
        bids=[(99.5, 12)],
        as_of=dt.datetime(2026, 11, 25, 8, 1, 35, tzinfo=KST),
    )
    book_mock = _setup(monkeypatch, tradability=_eligible(), orderbook=book)

    result = await build_tools()["get_quote"]("005930")

    book_mock.assert_awaited_once_with("005930", "kr", venue="nxt")
    assert result["price"] == pytest.approx(100.0)
    assert result["price_source"] == "nxt_mid"
    assert result["session"] == "nxt_premarket"
    assert result["regular_session_data_state"] == DATA_STATE_PREMARKET_UNAVAILABLE
    # KRX OHLC is carried from the prior close and says so.
    assert result["krx_ohlcv_basis"] == "carried_previous_close"
    assert result["open"] == pytest.approx(99.0)
    assert result["previous_close"] == pytest.approx(99.0)
    # The carried zero never reads as an observed trade count.
    assert result["volume"] is None
    assert result["value"] is None
    # NXT no-trade is explicit: the price is an orderbook mid, no NXT trade seen.
    assert result["nxt_trade_state"] == "no_trade_observed"
    assert result["price_as_of_kind"] == "time"
    assert result["price_usable"] is True
    assert result["data_state"] == "fresh"


# --- Acceptance 2: 069500 before 09:00 is premarket_unavailable and unusable ---


@pytest.mark.asyncio
async def test_premarket_not_nxt_tradable_symbol_is_unusable(monkeypatch):
    book_mock = _setup(
        monkeypatch,
        tradability=NxtTradability(
            nxt_eligible=False,
            nxt_trading_suspended=None,
            asof=NOW_PREMARKET,
        ),
    )

    result = await build_tools()["get_quote"]("069500")

    book_mock.assert_not_awaited()
    assert result["data_state"] == "premarket_unavailable"
    assert result["price_usable"] is False
    assert result["price_freshness"] == "stale"
    assert result["is_stale_price"] is True
    assert result["krx_ohlcv_basis"] == "carried_previous_close"
    assert result["volume"] is None
    assert "nxt_trade_state" not in result
    assert "session" not in result
    assert "price_source" not in result
    # The carried prior close stays visible, marked and unusable.
    assert result["price"] == pytest.approx(99.0)


@pytest.mark.asyncio
async def test_premarket_tradability_unknown_is_unusable(monkeypatch):
    book_mock = _setup(monkeypatch, tradability=None)

    result = await build_tools()["get_quote"]("069500")

    book_mock.assert_not_awaited()
    assert result["data_state"] == "premarket_unavailable"
    assert result["price_usable"] is False


@pytest.mark.asyncio
async def test_premarket_nxt_tradable_but_empty_book_stays_unusable(monkeypatch):
    book = _nxt_book(asks=[], bids=[], as_of=None, empty=True)
    _setup(monkeypatch, tradability=_eligible(), orderbook=book)

    result = await build_tools()["get_quote"]("005930")

    assert result["data_state"] == "premarket_unavailable"
    assert result["price_usable"] is False
    assert result["price_unavailable_reason"] == "date_only_price_asof"
    assert result["price_as_of_kind"] == "date_only"
    assert "nxt_trade_state" not in result
    assert "price_source" not in result


# --- Acceptance 3: date-only price_as_of is never fresh ------------------------


@pytest.mark.asyncio
async def test_premarket_date_only_price_asof_is_not_fresh_with_closed_reason(
    monkeypatch,
):
    _setup(monkeypatch, tradability=None)

    result = await build_tools()["get_quote"]("069500")

    assert result["price_as_of"] == "2026-11-25T00:00:00+09:00"
    assert result["price_as_of_kind"] == "date_only"
    assert result["price_freshness"] != "fresh"
    assert result["price_usable"] is False
    assert result["price_unavailable_reason"] == "date_only_price_asof"


@pytest.mark.parametrize(
    ("as_of", "expected"),
    [
        pytest.param("2026-11-25T00:00:00+09:00", "date_only", id="midnight-kst"),
        pytest.param("2026-11-25T08:01:35+09:00", "time", id="trade-time"),
        pytest.param("2026-11-25T00:00:01+09:00", "time", id="one-second-after"),
        pytest.param(None, "none", id="missing"),
        pytest.param("2026-11-25T00:00:00", "none", id="naive-rejected"),
    ],
)
def test_price_as_of_kind_separates_date_only_from_trade_time(as_of, expected):
    assert market_data_quotes._price_as_of_kind(as_of) == expected


def test_mark_premarket_date_only_quote_is_never_fresh():
    quote = {
        "price_as_of": "2026-11-25T00:00:00+09:00",
        "price_freshness": "fresh",
        "price_usable": True,
    }

    market_data_quotes._mark_premarket_price_unusable(quote)

    assert quote["price_freshness"] == "stale"
    assert quote["price_usable"] is False
    assert quote["is_stale_price"] is True
    assert quote["price_unavailable_reason"] == "date_only_price_asof"


# --- Regular session output keeps its previous shape ----------------------------


@pytest.mark.asyncio
async def test_regular_session_kr_quote_is_unchanged(monkeypatch):
    _setup(monkeypatch, tradability=None, state="fresh")

    result = await build_tools()["get_quote"]("005930")

    assert result["data_state"] == "fresh"
    assert result["price"] == pytest.approx(99.0)
    assert result["volume"] == 0
    assert result["price_freshness"] == "fresh"
    assert result["price_usable"] is True
    for key in (
        "krx_ohlcv_basis",
        "price_as_of_kind",
        "nxt_trade_state",
        "price_unavailable_reason",
    ):
        assert key not in result
