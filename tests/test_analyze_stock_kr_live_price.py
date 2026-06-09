# tests/test_analyze_stock_kr_live_price.py
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.mcp_server.tooling import analysis_analyze

KST = ZoneInfo("Asia/Seoul")


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
