"""hk 6498: KR get_quote price kind — 11-25 after-hours fixture, regular golden,
auction refusal through the order and auto-approve consumers.

All broker and DB reads are monkeypatched. Times are fixed KST instants.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes
from app.services import kr_quote_price
from app.services.kr_quote_price import PRICE_KIND_EXPECTED

KST = kr_quote_price.KST

# 2026-11-25 hk 6039 evidence (005930): after-hours, 18:16 KST.
NOW = dt.datetime(2026, 11, 25, 18, 16, tzinfo=KST)


def _daily_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-11-24",
                "open": 170000.0,
                "high": 171000.0,
                "low": 169000.0,
                "close": 170500.0,
                "volume": 900,
                "value": 150000000.0,
            },
            {
                "date": "2026-11-25",
                "open": 170600.0,
                "high": 171200.0,
                "low": 169000.0,
                "close": 169400.0,
                "volume": 1000,
                "value": 170000000.0,
            },
        ]
    )


class _KISClient:
    """Daily chart for the base quote; ``inquire_price`` is the NXT last trade."""

    def __init__(self, last_trade_time: dt.time | None, last_trade_close: float):
        self._last_trade_time = last_trade_time
        self._last_trade_close = last_trade_close

    async def inquire_daily_itemchartprice(self, code, market, n):
        return _daily_df()

    async def inquire_price(self, code, market):
        row = {
            "close": self._last_trade_close,
            "open": 0.0,
            "high": 0.0,
            "low": 0.0,
            "volume": 0,
            "value": 0,
            "date": dt.date(2026, 11, 25),
            "time": self._last_trade_time,
        }
        return pd.DataFrame([{"code": code, **row}]).set_index("code")


def _golden_daily_df() -> pd.DataFrame:
    """The exact two-row frame the pre-change golden was captured from (f20fad433)."""
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


def _nxt_snapshot(
    *,
    bid: float | None = 169100.0,
    ask: float | None = 169200.0,
    expected: int | None = 170800,
    as_of: dt.datetime = NOW,
):
    import app.services.market_data as market_data_service

    return market_data_service.OrderbookSnapshot(
        symbol="005930",
        instrument_type="equity_kr",
        source="kis",
        asks=(
            [market_data_service.OrderbookLevel(price=ask, quantity=10)]
            if ask is not None
            else []
        ),
        bids=(
            [market_data_service.OrderbookLevel(price=bid, quantity=20)]
            if bid is not None
            else []
        ),
        total_ask_qty=10.0,
        total_bid_qty=20.0,
        bid_ask_ratio=None,
        as_of=as_of,
        price_as_of_source="broker",
        expected_price=expected,
        expected_qty=None,
        venue="nxt",
        venue_label="NXT",
        kis_market_code="NX",
        source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        source_tr_id="FHKST01010200",
        is_empty_book=bid is None and ask is None,
        requires_final_recheck=False,
        empty_reason=None,
    )


def _patch_after_hours(monkeypatch, *, kis: _KISClient, snapshot) -> None:
    async def nxt_post(moment):
        return "nxt_after"

    async def no_tradability(symbols, db=None):
        return {}

    async def krx_after_lookup_failed(symbols, db=None):
        raise RuntimeError("no capability table in unit test")

    monkeypatch.setattr(market_data_quotes, "KISClient", lambda: kis)
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: NOW)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "market_closed"
    )
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_session_from_toss", nxt_post)
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", no_tradability)
    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", krx_after_lookup_failed
    )
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=snapshot),
    )


async def _quote() -> dict:
    return await market_data_quotes._get_quote_impl("005930", "kr")


@pytest.mark.asyncio
async def test_11_25_after_hours_price_is_last_trade_and_expected_is_kept(monkeypatch):
    """Acceptance 1: 18:11 trade 169,400 is the price; expected 170,800 kept apart."""
    kis = _KISClient(last_trade_time=dt.time(18, 11), last_trade_close=169400.0)
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price"] == pytest.approx(169400.0)
    assert quote["price_kind"] == "last_trade"
    assert quote["price_source"] == "nxt_last_trade"
    assert quote["price_session"] == "nxt_post"
    assert quote["expected_price"] == 170800
    assert quote["expected_price_venue"] == "nxt"
    assert quote["venues"]["nxt"]["best_bid"] == pytest.approx(169100.0)
    assert quote["venues"]["nxt"]["best_ask"] == pytest.approx(169200.0)
    assert quote["venues"]["nxt"]["observed_at"] == NOW.isoformat()
    assert quote["venues"]["nxt"]["last_trade_price"] == pytest.approx(169400.0)
    assert quote["price_as_of_source"] == "broker"


@pytest.mark.asyncio
async def test_11_25_after_hours_without_trade_uses_firm_mid(monkeypatch):
    """Acceptance 1, no-trade branch: the two-sided mid 169,150, never expected.

    The only trade reported is at 09:05, before the NXT post session starts, so
    it does not count as a trade in that session.
    """
    kis = _KISClient(last_trade_time=dt.time(9, 5), last_trade_close=168000.0)
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price"] == pytest.approx(169150.0)
    assert quote["price_kind"] == "mid"
    assert quote["price_source"] == "nxt_mid"
    assert quote["expected_price"] == 170800
    assert quote["price_as_of"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_after_hours_one_sided_book_is_unavailable_not_a_price(monkeypatch):
    kis = _KISClient(last_trade_time=None, last_trade_close=0.0)
    _patch_after_hours(
        monkeypatch,
        kis=kis,
        snapshot=_nxt_snapshot(bid=None, ask=169200.0, expected=None),
    )

    quote = await _quote()

    assert quote["price"] is None
    assert quote["price_kind"] == "unavailable"
    assert quote["price_kind_reason"] == "no_trade_and_one_sided_book"
    assert quote["price_usable"] is False


@pytest.mark.asyncio
async def test_regular_session_output_is_golden_plus_price_kind_keys(monkeypatch):
    """Acceptance 3: the KRX regular-session quote is the pre-change output.

    The golden below was captured from f20fad433 before this change. Only the
    two new keys (price_kind, price_session) are added.
    """

    class DailyOnly:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return _golden_daily_df()

    async def none_session(data_state, *, now=None):
        return None

    async def no_tradability(symbols, db=None):
        return {}

    async def krx_after_lookup_failed(symbols, db=None):
        raise RuntimeError("no capability table in unit test")

    monkeypatch.setattr(market_data_quotes, "KISClient", DailyOnly)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "fresh"
    )
    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", none_session)
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", no_tradability)
    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", krx_after_lookup_failed
    )

    quote = await market_data_quotes._get_quote_impl("005930", "kr")

    golden = {
        "data_state": "fresh",
        "high": 110.0,
        "instrument_type": "equity_kr",
        "is_stale_price": True,
        "krx_after_tradable": False,
        "krx_after_tradable_asof": None,
        "krx_after_tradable_list_source": None,
        "krx_after_tradable_reason": "lookup_failed",
        "krx_after_tradable_source": "krx_after_market_eligibility",
        "krx_after_tradable_stale": True,
        "low": 99.0,
        "open": 100.0,
        "previous_close": 100.0,
        "price": 105.0,
        "price_as_of": "2024-01-02T00:00:00+09:00",
        "price_freshness": "stale",
        "price_unavailable_reason": "stale_price_asof",
        "price_usable": False,
        "source": "kis",
        "symbol": "005930",
        "value": 105000.0,
        "volume": 1000,
    }
    added = {"price_kind": "last_trade", "price_session": "regular"}
    assert quote == {**golden, **added}


@pytest.mark.asyncio
async def test_auction_expected_is_refused_by_preflight_and_auto_approve(monkeypatch):
    """Acceptance 2: an auction-phase expected price is refused by both consumers."""
    from app.services.brokers.kiwoom.order_preflight import run_order_preflight
    from app.services.kiwoom_mock_place import fetch_kr_quote_for_preflight
    from app.services.order_proposals.auto_approve_price_fallback import (
        classify_kis_quote,
    )

    auction_quote = {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "source": "kis",
        "data_state": "fresh",
        "price": 170700.0,
        "is_stale_price": True,
        "price_freshness": "unavailable",
        "price_usable": False,
        "price_kind": PRICE_KIND_EXPECTED,
        "expected_price": 170700,
    }

    async def fake_get_quote(symbol, market):
        return auction_quote

    monkeypatch.setattr(market_data_quotes, "_get_quote_impl", fake_get_quote)

    # Order preflight (Kiwoom mock place path): no price basis, refused.
    quote_price, freshness = await fetch_kr_quote_for_preflight("005930")
    assert quote_price is None
    assert freshness == "unavailable"
    preflight = await run_order_preflight(
        account_client=None,
        symbol="005930",
        side="buy",
        quantity=1,
        price=170700,
        quote_price=quote_price,
        quote_freshness=freshness,
    )
    assert preflight.ok is False

    # Auto-approve price fallback: refused with the closed reason.
    fallback = classify_kis_quote(auction_quote, symbol="005930", market="equity_kr")
    assert fallback.price is None
    assert fallback.failure_reason == "expected_price_not_order_basis"


@pytest.mark.asyncio
async def test_expected_kind_from_overlay_is_marked_unusable(monkeypatch):
    """The overlay marks an expected price as unusable whatever its time."""
    overlay = {
        "price": 170700.0,
        "price_kind": PRICE_KIND_EXPECTED,
        "price_session": "auction",
        "expected_price": 170700,
        "expected_price_venue": "krx",
        "venues": {},
        "session": "nxt_after",
        "venue": "nxt",
        "price_source": "nxt_expected_price",
        "price_as_of": NOW.isoformat(),
        "price_as_of_source": "broker",
    }

    async def fake_overlay(symbol, *, session):
        return overlay

    async def fake_session(data_state, *, now=None):
        return "nxt_after"

    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: NOW)

    quote = {"symbol": "005930", "price": 169400.0, "source": "kis"}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="market_closed"
    )

    assert applied is True
    assert quote["price_kind"] == PRICE_KIND_EXPECTED
    assert quote["price_usable"] is False
    assert quote["price_freshness"] == "unavailable"
    assert quote["is_stale_price"] is True
    assert quote["price_unavailable_reason"] == "expected_price_not_order_basis"
