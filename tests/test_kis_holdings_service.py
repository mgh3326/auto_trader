"""Regression tests for KIS per-ticker holdings lookup (ROB-558).

The US path previously called a non-existent ``fetch_overseas_stocks`` which
raised AttributeError (silently swallowed → empty enrichment). These use a fake
client that ONLY exposes the real methods, so a wrong method name fails the test.
"""

import pytest

from app.models.manual_holdings import MarketType
from app.services.kis_holdings_service import get_kis_holding_for_ticker


class _FakeKIS:
    """Fake KISClient exposing only the real holding methods."""

    def __init__(self, overseas=None, domestic=None):
        self._overseas = overseas or []
        self._domestic = domestic or []

    async def fetch_my_overseas_stocks(self, *args, **kwargs):
        return self._overseas

    async def fetch_my_stocks(self, *args, **kwargs):
        return self._domestic


@pytest.mark.unit
@pytest.mark.asyncio
async def test_us_holding_uses_fetch_my_overseas_stocks():
    client = _FakeKIS(
        overseas=[
            {
                "ovrs_pdno": "AAPL",
                "ovrs_cblc_qty": "5",
                "pchs_avg_pric": "190.5",
                "now_pric2": "200.0",
            }
        ]
    )
    result = await get_kis_holding_for_ticker(client, "AAPL", MarketType.US)
    assert result["quantity"] == 5
    assert result["avg_price"] == pytest.approx(190.5)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_holding_uses_fetch_my_stocks():
    client = _FakeKIS(
        domestic=[
            {
                "pdno": "005930",
                "hldg_qty": "10",
                "pchs_avg_pric": "68000",
                "prpr": "68500",
            }
        ]
    )
    result = await get_kis_holding_for_ticker(client, "005930", MarketType.KR)
    assert result["quantity"] == 10
    assert result["avg_price"] == pytest.approx(68000.0)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_ticker_returns_default():
    client = _FakeKIS(overseas=[])
    result = await get_kis_holding_for_ticker(client, "TSLA", MarketType.US)
    assert result == {"quantity": 0, "avg_price": 0.0, "current_price": 0.0}
