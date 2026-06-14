from unittest.mock import AsyncMock, patch

import pytest

from app.services.fill_enrichment import fetch_fill_enrichment
from app.services.fill_notification import FillOrder


def _kr(side="ask"):
    return FillOrder(symbol="005930", side=side, filled_price=68500.0, filled_qty=10.0,
                     filled_amount=685000.0, filled_at="t", account="kis",
                     market_type="kr", currency="KRW")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_sell_realized_pnl(monkeypatch):
    async def fake_holding(client, ticker, market):
        return {"quantity": 50, "avg_price": 68000.0, "current_price": 68500.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", fake_holding)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    enr = await fetch_fill_enrichment(_kr(side="ask"))
    assert enr is not None
    # (68500-68000)*10 = 5000
    assert enr.realized_pnl_amount == pytest.approx(5000.0)
    assert enr.realized_pnl_rate == pytest.approx((68500/68000 - 1) * 100)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_kr_buy_position(monkeypatch):
    async def fake_holding(client, ticker, market):
        return {"quantity": 30, "avg_price": 68100.0, "current_price": 68500.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", fake_holding)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    enr = await fetch_fill_enrichment(_kr(side="bid"))
    assert enr is not None
    assert enr.position_qty == pytest.approx(30.0)
    assert enr.position_avg_price == pytest.approx(68100.0)
    assert enr.realized_pnl_amount is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_fail_open_returns_none(monkeypatch):
    async def boom(client, ticker, market):
        raise RuntimeError("broker down")

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", boom)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    assert await fetch_fill_enrichment(_kr()) is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_position_returns_none(monkeypatch):
    async def empty(client, ticker, market):
        return {"quantity": 0, "avg_price": 0.0, "current_price": 0.0}

    monkeypatch.setattr("app.services.fill_enrichment.get_kis_holding_for_ticker", empty)
    monkeypatch.setattr("app.services.fill_enrichment.KISClient", lambda: object())
    assert await fetch_fill_enrichment(_kr()) is None
