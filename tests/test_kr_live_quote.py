"""ROB-396 follow-up: direct coverage for `_fetch_kr_live_quote`.

The analyze-path tests monkeypatch `_fetch_kr_live_quote` out, so its real body
(KIS `inquire_price` call + row parsing + as_of composition) was uncovered.
These tests exercise the body directly with a faked `KISClient.inquire_price`.
"""

import datetime

import pandas as pd
import pytest

from app.mcp_server.tooling import market_data_quotes


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
    assert quote["price_as_of"] == "2026-06-01T09:30:00"


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_as_of_without_time_uses_date(monkeypatch):
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
    assert quote["price_as_of"] == "2026-06-01T00:00:00"


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_empty_df_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(pd.DataFrame()))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None


@pytest.mark.asyncio
async def test_fetch_kr_live_quote_swallows_kis_error_returns_none(monkeypatch):
    monkeypatch.setattr(market_data_quotes, "KISClient", _make_kis(raises=True))

    assert await market_data_quotes._fetch_kr_live_quote("012450") is None
