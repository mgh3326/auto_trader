# tests/test_analyze_stock_kr_live_price.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze

KST = ZoneInfo("Asia/Seoul")


@pytest.fixture(autouse=True)
def _no_nxt_overlay_by_default(monkeypatch):
    """ROB-725: keep _resolve_kr_quote tests hermetic.

    Without this guard, tests that don't mock the overlay would trigger the REAL
    _apply_nxt_quote_overlay during the 15:30–20:00 KST NXT-after wall-clock
    window (session detection is wall-clock gated, not data_state gated), firing
    a live get_orderbook network call that can overwrite the mocked price. Tests
    that exercise the overlay set their own fake, which overrides this default.
    """

    async def _noop(symbol, quote, *, data_state):
        return False

    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", _noop)


def _ohlcv():
    # 전일 일봉(어제 날짜) — fallback 경로용
    yesterday = pd.Timestamp(datetime.now(KST).date() - timedelta(days=1))
    return pd.DataFrame(
        {
            "open": [100.0],
            "high": [110.0],
            "low": [90.0],
            "close": [105.0],
            "volume": [1000],
            "value": [105000.0],
        },
        index=[yesterday],
    )


@pytest.mark.asyncio
async def test_kr_live_price_today_is_not_stale(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 1225000.0,
            "open": 1200000.0,
            "high": 1230000.0,
            "low": 1190000.0,
            "volume": 5,
            "value": 6,
            "source": "kis",
            "price_as_of": today.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 1225000.0
    assert quote["is_stale_price"] is False


@pytest.mark.asyncio
async def test_kr_prev_day_quote_is_stale(monkeypatch):
    prev = datetime.now(KST) - timedelta(days=1)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 1173000.0,
            "open": 1.0,
            "high": 1.0,
            "low": 1.0,
            "volume": 1,
            "value": 1,
            "source": "kis",
            "price_as_of": prev.isoformat(),
        }

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["is_stale_price"] is True


@pytest.mark.asyncio
async def test_kr_live_failure_falls_back_to_ohlcv_stale(monkeypatch):
    async def fake_live(symbol):
        return None  # inquire_price 실패/빈응답

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    quote = await analysis_analyze._resolve_kr_quote("012450", _ohlcv())
    assert quote["price"] == 105.0  # 일봉 종가 fallback
    assert quote["is_stale_price"] is True
    assert quote["price_as_of"] is not None


@pytest.mark.asyncio
async def test_kr_quote_overlays_nxt_price_in_premarket(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,  # stale KRX prior close
            "source": "kis",
            "price_as_of": (today - timedelta(days=1)).isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        quote["price"] = 173500.0
        quote["price_source"] = "nxt_expected_price"
        quote["session"] = "nxt_premarket"
        quote["data_state"] = "fresh"
        return True

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        analysis_analyze,
        "kr_market_data_state",
        lambda *a, **k: "premarket_unavailable",
    )

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 173500.0
    assert quote["price_source"] == "nxt_expected_price"
    assert quote["is_stale_price"] is False  # overlay price is fresh
    # price_as_of refreshed to the live NXT fetch time (today, not yesterday)
    assert quote["price_as_of"].startswith(str(today.date()))


@pytest.mark.asyncio
async def test_kr_quote_keeps_kis_price_when_no_overlay(monkeypatch):
    today = datetime.now(KST)

    async def fake_live(symbol):
        return {
            "symbol": symbol,
            "instrument_type": "equity_kr",
            "price": 168300.0,
            "source": "kis",
            "price_as_of": today.isoformat(),
        }

    async def fake_overlay(symbol, quote, *, data_state):
        return False  # not an NXT session / empty book

    monkeypatch.setattr(analysis_analyze, "_fetch_kr_live_quote", fake_live)
    monkeypatch.setattr(analysis_analyze, "_apply_nxt_quote_overlay", fake_overlay)
    monkeypatch.setattr(
        analysis_analyze, "kr_market_data_state", lambda *a, **k: "fresh"
    )

    quote = await analysis_analyze._resolve_kr_quote("192820", _ohlcv())

    assert quote["price"] == 168300.0
    assert "price_source" not in quote
    assert quote["is_stale_price"] is False  # today's KIS as_of, unchanged
