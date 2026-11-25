"""get_market_index: SOX / KOSPI200 / NQ=F / ES=F / USDKRW / KOSPI200_NIGHT rows and
KR OHLC date separation (task 6301). All provider responses are fakes."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import AsyncMock

import httpx
import pytest

import app.mcp_server.tooling.fundamentals._market_index as mkt
from app.mcp_server.tooling.fundamentals_sources_indices import (
    _fetch_index_kr_current,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _us_row(symbol: str, name: str) -> dict:
    return {
        "symbol": symbol,
        "name": name,
        "current": 123.45,
        "change": 1.23,
        "change_pct": 1.0,
        "open": 122.0,
        "high": 124.0,
        "low": 121.0,
        "volume": 1000,
        "source": "yfinance",
    }


@pytest.fixture
def yahoo_fake(monkeypatch):
    monkeypatch.setattr(
        mkt,
        "_fetch_index_us_current",
        AsyncMock(side_effect=lambda ticker, name, symbol: _us_row(symbol, name)),
    )
    monkeypatch.setattr(mkt, "_fetch_index_us_history", AsyncMock(return_value=[]))
    return mkt._fetch_index_us_current


@pytest.mark.parametrize(
    ("symbol", "yf_ticker"),
    [("SOX", "^SOX"), ("NQ=F", "NQ=F"), ("ES=F", "ES=F"), ("USDKRW", "KRW=X")],
)
async def test_yahoo_symbols_carry_value_source_and_utc_as_of(
    yahoo_fake, symbol, yf_ticker
):
    result = await mkt.handle_get_market_index(symbol=symbol)

    row = result["indices"][0]
    assert row["symbol"] == symbol
    assert row["current"] == pytest.approx(123.45)
    assert row["source"] == "yfinance"
    assert row["as_of"].endswith("+00:00")
    assert row["as_of_basis"] == "fetched_at"
    assert row["session_state"]
    # the Yahoo ticker actually used
    assert yahoo_fake.await_args.args[0] == yf_ticker


async def test_continuous_futures_carry_contract_and_unverified_delay(yahoo_fake):
    for symbol, yf_ticker in (("NQ=F", "NQ=F"), ("ES=F", "ES=F")):
        row = (await mkt.handle_get_market_index(symbol=symbol))["indices"][0]
        assert row["contract"] == {
            "kind": "continuous",
            "yahoo_symbol": yf_ticker,
            "note": row["contract"]["note"],
        }
        assert row["session_state"] == "continuous_futures"
        assert row["delayed"] is None
        assert row["delay_basis"] == "not_verified"


async def test_usdkrw_fx_row_is_a_continuous_fx_state(yahoo_fake):
    row = (await mkt.handle_get_market_index(symbol="USDKRW"))["indices"][0]
    assert row["session_state"] == "continuous_fx"
    assert "contract" not in row


def _naver_fake(monkeypatch, basic: dict, price_rows: list[dict]):
    async def fake_get(self_cli, url, **kwargs):
        if url.endswith("/basic"):
            return _Resp(basic)
        if url.endswith("/price"):
            return _Resp(price_rows)
        raise AssertionError(f"unexpected URL {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


async def test_kospi200_uses_naver_kpi200_and_reports_requested_symbol(monkeypatch):
    _naver_fake(
        monkeypatch,
        {
            "closePrice": "350.10",
            "compareToPreviousClosePrice": "1.00",
            "fluctuationsRatio": "0.29",
            "localTradedAt": "2026-11-25T14:30:00+09:00",
        },
        [
            {
                "localTradedAt": "2026-11-25",
                "openPrice": "349.00",
                "highPrice": "351.00",
                "lowPrice": "348.00",
                "accumulatedTradingVolume": "1000",
            }
        ],
    )
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "market_closed")
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock(return_value=[]))

    row = (await mkt.handle_get_market_index(symbol="KOSPI200"))["indices"][0]

    assert row["symbol"] == "KOSPI200"
    assert row["source"] == "naver"
    assert row["current"] == pytest.approx(350.10)
    assert row["as_of"] == "2026-11-25T05:30:00+00:00"
    assert row["as_of_basis"] == "quote_time"


async def test_kospi_current_from_today_and_ohlc_from_previous_session_are_separated(
    monkeypatch,
):
    """Acceptance 3: the current value is today's; the OHLC is the previous
    session's. The record must not present them as one session."""
    _naver_fake(
        monkeypatch,
        {
            "closePrice": "8100.00",
            "compareToPreviousClosePrice": "-20.00",
            "fluctuationsRatio": "-0.25",
            "localTradedAt": "2026-11-25T10:00:00+09:00",
        },
        [
            {
                "localTradedAt": "2026-11-24",
                "openPrice": "8200.00",
                "highPrice": "8250.00",
                "lowPrice": "8150.00",
                "accumulatedTradingVolume": "450000000",
            }
        ],
    )
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "market_closed")
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock(return_value=[]))

    row = (await mkt.handle_get_market_index(symbol="KOSPI"))["indices"][0]

    assert row["current"] == pytest.approx(8100.00)
    assert row["session_date"] == "2026-11-25"
    assert row["ohlc_date"] == "2026-11-24"
    assert row["as_of"] == "2026-11-25T01:00:00+00:00"
    # top-level OHLC belongs to no current session -> null
    assert row["open"] is None and row["high"] is None
    assert row["low"] is None and row["volume"] is None
    assert row["previous_session"] == {
        "date": "2026-11-24",
        "open": pytest.approx(8200.00),
        "high": pytest.approx(8250.00),
        "low": pytest.approx(8150.00),
        "volume": 450000000,
    }


async def test_kospi_same_session_ohlc_stays_top_level(monkeypatch):
    _naver_fake(
        monkeypatch,
        {
            "closePrice": "8100.00",
            "compareToPreviousClosePrice": "-20.00",
            "fluctuationsRatio": "-0.25",
            "localTradedAt": "2026-11-25T10:00:00+09:00",
        },
        [
            {
                "localTradedAt": "2026-11-25",
                "openPrice": "8120.00",
                "highPrice": "8130.00",
                "lowPrice": "8090.00",
                "accumulatedTradingVolume": "1",
            }
        ],
    )
    fetched = await _fetch_index_kr_current("KOSPI", "코스피")

    assert fetched["open"] == pytest.approx(8120.00)
    assert "previous_session" not in fetched


async def test_kospi200_night_futures_gate_off_is_unsupported_without_kis_call(
    monkeypatch,
):
    monkeypatch.delenv("KIS_FUTURES_QUOTES_ENABLED", raising=False)

    # The handler never reaches the regular-session futures quote view (review r1).
    assert not hasattr(mkt, "open_quotes_view")

    result = await mkt.handle_get_market_index(symbol="KOSPI200_NIGHT")

    row = result["indices"][0]
    assert row["unsupported"] is True
    assert row["current"] is None
    assert "KIS_FUTURES_QUOTES_ENABLED" in row["unsupported_reason"]
    assert row["as_of_basis"] == "fetched_at"


async def test_kospi200_night_futures_gate_on_still_unsupported_and_reads_no_quote(
    monkeypatch,
):
    """Review finding (high): the futures view's price TR is the regular F market,
    so with the gate on it would return the day session under a night symbol. The
    row must stay unsupported and no KIS quote may be read."""
    monkeypatch.setenv("KIS_FUTURES_QUOTES_ENABLED", "true")

    assert not hasattr(mkt, "open_quotes_view")

    row = (await mkt.handle_get_market_index(symbol="KOSPI200_NIGHT"))["indices"][0]

    assert row["unsupported"] is True
    assert row["current"] is None
    assert "day session" in row["unsupported_reason"]
    assert "contract" not in row


async def test_unknown_symbol_error_lists_supported_symbols():
    with pytest.raises(ValueError, match="Unknown index symbol 'NOPE'") as info:
        await mkt.handle_get_market_index(symbol="NOPE")
    message = str(info.value)
    for supported in ("SOX", "KOSPI200", "NQ=F", "ES=F", "USDKRW", "KOSPI200_NIGHT"):
        assert supported in message


async def test_kr_frozen_lagging_payload_stamps_utc_as_of_and_basis(monkeypatch):
    """Review finding (medium): the ROB-464 branch must not leave a KST as_of
    without a basis."""
    import app.mcp_server.tooling.fundamentals._market_index as mkt_mod

    _naver_fake(
        monkeypatch,
        {
            "closePrice": "8123.62",
            "compareToPreviousClosePrice": "0",
            "fluctuationsRatio": "0",
        },
        [
            {
                "localTradedAt": "2026-11-25",
                "openPrice": "8263.85",
                "highPrice": "8434.40",
                "lowPrice": "8079.77",
                "accumulatedTradingVolume": "450000000",
            }
        ],
    )
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "fresh")
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        mkt_mod,
        "now_kst",
        lambda: datetime(2026, 11, 25, 10, 0, 0, tzinfo=mkt_mod.KST),
    )

    row = (await mkt.handle_get_market_index(symbol="KOSPI"))["indices"][0]

    assert row["data_state"] == "stale"
    assert row["as_of"] == "2026-11-25T01:00:00+00:00"
    assert row["as_of_basis"] == "fetched_at"


async def test_kr_frozen_payload_stays_stale_when_ohlc_is_previous_session(
    monkeypatch,
):
    """Review finding (medium): with a date split, open is null at top level. The
    frozen-payload check must still fire on the previous session's open."""
    import app.mcp_server.tooling.fundamentals._market_index as mkt_mod

    _naver_fake(
        monkeypatch,
        {
            "closePrice": "8123.62",
            "compareToPreviousClosePrice": "0",
            "fluctuationsRatio": "0",
            "localTradedAt": "2026-11-25T10:00:00+09:00",
        },
        [
            {
                "localTradedAt": "2026-11-24",
                "openPrice": "8263.85",
                "highPrice": "8434.40",
                "lowPrice": "8079.77",
                "accumulatedTradingVolume": "450000000",
            }
        ],
    )
    monkeypatch.setattr(mkt, "kr_market_data_state", lambda *a, **k: "fresh")
    monkeypatch.setattr(mkt, "_fetch_index_kr_history", AsyncMock(return_value=[]))
    # 30 s after the quote: inside the lag threshold, so only the frozen-payload
    # check can downgrade the row.
    monkeypatch.setattr(
        mkt_mod,
        "now_kst",
        lambda: datetime(2026, 11, 25, 10, 0, 30, tzinfo=mkt_mod.KST),
    )

    row = (await mkt.handle_get_market_index(symbol="KOSPI"))["indices"][0]

    assert row["open"] is None
    assert row["previous_session"]["open"] == pytest.approx(8263.85)
    assert row["data_state"] == "stale"
    assert row["data_state_reason"] == "kr_index_fresh_clock_payload_lagging"
