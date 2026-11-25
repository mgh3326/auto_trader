"""hk 6498: KR get_quote price kind through the real quote path.

Covers the 11-25 after-hours fixture (NXT minute-chart last trade), a stale last
trade, the KRX closing auction driven through ``_get_quote_impl`` (no hand-built
quote), the regular-session golden, and the consumer refusals.

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
AUCTION_NOW = dt.datetime(2026, 11, 25, 15, 25, tzinfo=KST)
REGULAR_NOW = dt.datetime(2026, 11, 25, 11, 0, tzinfo=KST)


def _kst(hour: int, minute: int) -> dt.datetime:
    return dt.datetime(2026, 11, 25, hour, minute, tzinfo=KST)


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


def _minute_df(bars: list[tuple[int, int, float, int]]) -> pd.DataFrame:
    """KIS minute-chart frame shape: datetime (naive KST bar start), date, time,
    close, volume; ascending by datetime, as ``_build_ohlcv_dataframe`` returns it."""
    rows = []
    for hour, minute, close, volume in bars:
        stamp = pd.Timestamp(2026, 11, 25, hour, minute)
        rows.append(
            {
                "datetime": stamp,
                "date": stamp.date(),
                "time": stamp.time(),
                "open": close,
                "high": close,
                "low": close,
                "close": float(close),
                "volume": volume,
                "value": 0,
            }
        )
    return pd.DataFrame(rows)


class _KISClient:
    """Daily chart for the base quote; ``inquire_minute_chart`` gives the NX bars."""

    def __init__(self, minute_bars: pd.DataFrame | None):
        self._minute_bars = minute_bars

    async def inquire_daily_itemchartprice(self, code, market, n):
        return _daily_df()

    async def inquire_minute_chart(self, code, market, time_unit, n):
        assert market == "NX"
        assert time_unit == 1
        if self._minute_bars is None:
            raise RuntimeError("no minute chart in this fixture")
        return self._minute_bars


def _book_snapshot(
    *,
    venue: str,
    kis_market_code: str,
    bid: float | None,
    ask: float | None,
    expected: int | None,
    as_of: dt.datetime,
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
        venue=venue,
        venue_label=venue.upper(),
        kis_market_code=kis_market_code,
        source_endpoint="/uapi/domestic-stock/v1/quotations/inquire-asking-price-exp-ccn",
        source_tr_id="FHKST01010200",
        is_empty_book=bid is None and ask is None,
        requires_final_recheck=False,
        empty_reason=None,
    )


def _nxt_snapshot(
    *,
    bid: float | None = 169100.0,
    ask: float | None = 169200.0,
    expected: int | None = 170800,
    as_of: dt.datetime = NOW,
):
    return _book_snapshot(
        venue="nxt",
        kis_market_code="NX",
        bid=bid,
        ask=ask,
        expected=expected,
        as_of=as_of,
    )


def _patch_common(monkeypatch, *, kis: _KISClient, now: dt.datetime) -> None:
    async def no_tradability(symbols, db=None):
        return {}

    async def krx_after_lookup_failed(symbols, db=None):
        raise RuntimeError("no capability table in unit test")

    monkeypatch.setattr(market_data_quotes, "KISClient", lambda: kis)
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: now)
    monkeypatch.setattr(market_data_quotes, "is_kr_session_day", lambda date: True)
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_tradability", no_tradability)
    monkeypatch.setattr(
        market_data_quotes, "get_kr_krx_after_tradability", krx_after_lookup_failed
    )


def _patch_after_hours(monkeypatch, *, kis: _KISClient, snapshot) -> None:
    async def nxt_post(moment):
        return "nxt_after"

    _patch_common(monkeypatch, kis=kis, now=NOW)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "market_closed"
    )
    monkeypatch.setattr(market_data_quotes, "get_kr_nxt_session_from_toss", nxt_post)
    monkeypatch.setattr(
        market_data_quotes.market_data_service,
        "get_orderbook",
        AsyncMock(return_value=snapshot),
    )


async def _quote() -> dict:
    return await market_data_quotes._get_quote_impl("005930", "kr")


@pytest.mark.asyncio
async def test_11_25_after_hours_price_is_last_trade_and_expected_is_kept(monkeypatch):
    """Acceptance 1: the 18:11 bar (169,400) is the last trade; expected 170,800 kept."""
    kis = _KISClient(_minute_df([(18, 9, 169300.0, 120), (18, 11, 169400.0, 300)]))
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price"] == pytest.approx(169400.0)
    assert quote["price_kind"] == "last_trade"
    assert quote["price_source"] == "nxt_last_trade"
    assert quote["price_session"] == "nxt_post"
    assert quote["price_as_of"] == _kst(18, 11).isoformat()
    assert quote["price_usable"] is True
    assert quote["expected_price"] == 170800
    assert quote["expected_price_venue"] == "nxt"
    assert quote["venues"]["nxt"]["best_bid"] == pytest.approx(169100.0)
    assert quote["venues"]["nxt"]["best_ask"] == pytest.approx(169200.0)
    assert quote["venues"]["nxt"]["observed_at"] == NOW.isoformat()
    assert quote["venues"]["nxt"]["last_trade_price"] == pytest.approx(169400.0)


@pytest.mark.asyncio
async def test_11_25_after_hours_without_trade_uses_firm_mid(monkeypatch):
    """Acceptance 1, no-trade branch: the two-sided mid 169,150, never expected.

    The only bar with volume is at 09:05, before the NXT post session starts.
    """
    kis = _KISClient(_minute_df([(9, 5, 168000.0, 500), (18, 10, 169300.0, 0)]))
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price"] == pytest.approx(169150.0)
    assert quote["price_kind"] == "mid"
    assert quote["price_source"] == "nxt_mid"
    assert quote["expected_price"] == 170800
    assert quote["price_as_of"] == NOW.isoformat()


@pytest.mark.asyncio
async def test_stale_last_trade_falls_back_to_live_mid(monkeypatch):
    """A trade older than 5 minutes is not a live price; the live two-sided mid wins."""
    kis = _KISClient(_minute_df([(17, 0, 169500.0, 400)]))
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price_kind"] == "mid"
    assert quote["price"] == pytest.approx(169150.0)
    assert quote["price_usable"] is True


@pytest.mark.asyncio
async def test_last_trade_read_failure_is_mid_not_unavailable(monkeypatch):
    kis = _KISClient(None)
    _patch_after_hours(monkeypatch, kis=kis, snapshot=_nxt_snapshot())

    quote = await _quote()

    assert quote["price_kind"] == "mid"
    assert quote["price"] == pytest.approx(169150.0)


@pytest.mark.asyncio
async def test_after_hours_one_sided_book_is_unavailable_not_a_price(monkeypatch):
    kis = _KISClient(_minute_df([(9, 5, 168000.0, 500)]))
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
async def test_krx_closing_auction_uses_expected_price_through_real_quote_path(
    monkeypatch,
):
    """Acceptance 2 through ``_get_quote_impl``: 15:25 is in the KRX closing auction.

    The KRX book's expected price (170,700) is the representative price but is
    not an order basis, so the Kiwoom preflight and auto-approve refuse it.
    """
    from app.services.brokers.kiwoom.order_preflight import run_order_preflight
    from app.services.kiwoom_mock_place import fetch_kr_quote_for_preflight
    from app.services.order_proposals.auto_approve_price_fallback import (
        classify_kis_quote,
    )

    kis = _KISClient(None)
    _patch_common(monkeypatch, kis=kis, now=AUCTION_NOW)
    krx_book = _book_snapshot(
        venue="krx",
        kis_market_code="J",
        bid=170600.0,
        ask=170700.0,
        expected=170700,
        as_of=AUCTION_NOW,
    )
    get_orderbook = AsyncMock(return_value=krx_book)
    monkeypatch.setattr(
        market_data_quotes.market_data_service, "get_orderbook", get_orderbook
    )

    quote = await _quote()

    get_orderbook.assert_awaited_once_with("005930", "kr", venue="krx")
    assert quote["price_kind"] == PRICE_KIND_EXPECTED
    assert quote["price"] == pytest.approx(170700.0)
    assert quote["expected_price"] == 170700
    assert quote["expected_price_venue"] == "krx"
    assert quote["price_session"] == "auction"
    assert quote["price_usable"] is False
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_unavailable_reason"] == "expected_price_not_order_basis"

    async def fake_get_quote(symbol, market):
        return quote

    monkeypatch.setattr(market_data_quotes, "_get_quote_impl", fake_get_quote)
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

    fallback = classify_kis_quote(quote, symbol="005930", market="equity_kr")
    assert fallback.price is None
    assert fallback.failure_reason == "expected_price_not_order_basis"


@pytest.mark.asyncio
async def test_krx_auction_read_failure_keeps_base_quote(monkeypatch):
    kis = _KISClient(None)
    _patch_common(monkeypatch, kis=kis, now=AUCTION_NOW)
    monkeypatch.setattr(
        market_data_quotes, "kr_market_data_state", lambda *a, **k: "market_closed"
    )

    async def broken(symbol, market, venue=None):
        raise RuntimeError("orderbook down")

    monkeypatch.setattr(market_data_quotes.market_data_service, "get_orderbook", broken)

    quote = await _quote()

    assert quote["data_state"] == "market_closed"
    assert "price_kind" not in quote or quote["price_kind"] == "unavailable"


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
    monkeypatch.setattr(market_data_quotes, "now_kst", lambda: REGULAR_NOW)
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
