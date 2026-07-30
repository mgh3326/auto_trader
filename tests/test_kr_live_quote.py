"""ROB-396 follow-up: direct coverage for `_fetch_kr_live_quote`.

The analyze-path tests monkeypatch `_fetch_kr_live_quote` out, so its real body
(KIS `inquire_price` call + row parsing + as_of composition) was uncovered.
These tests exercise the body directly with a faked `KISClient.inquire_price`.
"""

import datetime

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes
from app.services.brokers.kis.domestic_market_data import DomesticMarketDataMixin


def _make_kis(df=None, *, raises=False):
    class _FakeKIS:
        def __init__(self, *args, **kwargs):
            pass

        async def inquire_price(self, code, market="J"):
            if raises:
                raise RuntimeError("kis down")
            return df

    return _FakeKIS


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_parses_price_and_as_of(monkeypatch):
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-01"),
                "time": datetime.time(9, 30, 0),
                "open": 1200000.0,
                "high": 1230000.0,
                "low": 1190000.0,
                "close": 1225000.0,
                "volume": 5,
                "value": 6,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price"] == 1225000.0  # stck_prpr → close (live, not prev close)
    assert quote["source"] == "kis"
    assert quote["instrument_type"] == "equity_kr"
    assert quote["price_as_of"] == "2026-06-01T09:30:00+09:00"
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_as_of_without_time_is_unavailable(monkeypatch):
    # ROB-1121: provider가 time을 주지 않으면 날짜만으로 자정을 합성하지 않는다.
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2026-06-01"),
                "time": None,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1225000.0,
                "volume": 1,
                "value": 1,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_as_of_missing_date_time_is_unavailable(monkeypatch):
    # ROB-1121: provider가 date와 time 둘 다 주지 않으면 price_as_of는 None.
    df = pd.DataFrame(
        [
            {
                "date": None,
                "time": None,
                "open": 1.0,
                "high": 1.0,
                "low": 1.0,
                "close": 1225000.0,
                "volume": 1,
                "value": 1,
            }
        ],
        index=["012450"],
    )
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(df))

    quote = await market_data_quotes._fetch_kr_live_quote("012450")

    assert quote is not None
    assert quote["price_as_of"] is None
    assert quote["fetched_at"] is not None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_empty_df_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(pd.DataFrame()))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_swallows_kis_error_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(raises=True))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


class _DummyMarketData(DomesticMarketDataMixin):
    def __init__(self, raw_output):
        self._raw_output = raw_output

    def _kis_url(self, path: str) -> str:
        return f"https://mock.kis/{path}"

    async def _request_with_token_retry(self, **kwargs):
        return {"output": self._raw_output}


@pytest.mark.asyncio
async def test_inquire_price_parser_does_not_synthesize_local_clock():
    raw_payload = {
        "stck_shrn_iscd": "005930",
        "stck_bsop_date": "",
        "stck_cntg_hour": "",
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_prpr": "105",
        "acml_vol": "1000",
        "acml_tr_pbmn": "105000",
    }
    client = _DummyMarketData(raw_payload)
    df = await client.inquire_price("005930")

    assert not df.empty
    row = df.iloc[0].to_dict()
    assert row["date"] is None
    assert row["time"] is None


@pytest.mark.asyncio
async def test_inquire_price_parser_date_only():
    raw_payload = {
        "stck_shrn_iscd": "005930",
        "stck_bsop_date": "20260601",
        "stck_cntg_hour": "",
        "stck_oprc": "100",
        "stck_hgpr": "110",
        "stck_lwpr": "90",
        "stck_prpr": "105",
        "acml_vol": "1000",
        "acml_tr_pbmn": "105000",
    }
    client = _DummyMarketData(raw_payload)
    df = await client.inquire_price("005930")

    assert not df.empty
    row = df.iloc[0].to_dict()
    assert row["date"] == pd.Timestamp("2026-06-01")
    assert row["time"] is None


def test_annotate_kr_price_freshness_timezone_naive_is_unavailable():
    naive_as_of = datetime.datetime(2026, 6, 1, 9, 30, 0)
    quote = {}
    market_data_quotes._annotate_kr_price_freshness(quote, naive_as_of)

    assert quote["price_as_of"] is None
    assert quote["is_stale_price"] is True
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["price_unavailable_reason"] == "missing_price_asof"


@pytest.mark.asyncio
async def test_apply_nxt_quote_overlay_stamps_unavailable_freshness(monkeypatch):
    async def fake_session(data_state, *, now=None):
        return "nxt_premarket"

    async def fake_overlay(symbol, *, session):
        return {
            "price": 105.0,
            "session": session,
            "venue": "nxt",
            "price_source": "nxt_expected_price",
        }

    monkeypatch.setattr(market_data_quotes, "_nxt_quote_session", fake_session)
    monkeypatch.setattr(market_data_quotes, "_fetch_nxt_quote_overlay", fake_overlay)

    quote = {"price": 100.0}
    applied = await market_data_quotes._apply_nxt_quote_overlay(
        "005930", quote, data_state="premarket_unavailable"
    )

    assert applied is True
    assert quote["price"] == 105.0
    assert quote["price_as_of"] is None
    assert quote["price_freshness"] == "unavailable"
    assert quote["price_usable"] is False
    assert quote["is_stale_price"] is True
    assert quote["price_unavailable_reason"] == "missing_price_asof"
