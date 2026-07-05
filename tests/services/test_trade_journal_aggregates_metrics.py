from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.services.trade_journal import aggregates as agg
from app.services.trade_journal.aggregates import ClosedTrade, compute_r_multiple


def _trade(**kw):
    base = {
        "market": "kr",
        "symbol": "005930",
        "account": "acct",
        "qty": 10,
        "entry_price": 100.0,
        "exit_price": 112.0,
        "entry_ts": datetime(2026, 6, 1, tzinfo=UTC),
        "exit_ts": datetime(2026, 6, 5, tzinfo=UTC),
        "pnl_abs": 120.0,
        "pnl_pct": 0.12,
        "fees": 0.0,
        "entry_item_uuids": (),
        "exit_item_uuid": None,
        "entry_correlation_ids": (),
        "exit_correlation_id": None,
    }
    base.update(kw)
    return ClosedTrade(**base)


def test_r_multiple_with_stop():
    # entry 100, stop 96 -> risk 4; exit 112 -> reward 12 -> R = 3.0
    assert compute_r_multiple(_trade(), 96.0) == pytest.approx(3.0)


def test_r_multiple_none_without_stop():
    assert compute_r_multiple(_trade(), None) is None


@pytest.mark.asyncio
async def test_excursions_from_stubbed_ohlcv(monkeypatch):
    @dataclass
    class C:
        timestamp: datetime
        high: float
        low: float

    async def fake_get_ohlcv(symbol, market, period, count, end=None):
        return [
            C(datetime(2026, 6, 1, tzinfo=UTC), 101, 95),   # low 95
            C(datetime(2026, 6, 3, tzinfo=UTC), 118, 99),   # high 118
            C(datetime(2026, 6, 5, tzinfo=UTC), 113, 108),
        ]

    monkeypatch.setattr(agg, "get_ohlcv", fake_get_ohlcv)
    mae, mfe, degraded = await agg.compute_excursions(_trade())
    assert mae == pytest.approx((95 - 100) / 100)   # -0.05
    assert mfe == pytest.approx((118 - 100) / 100)   # +0.18
    assert degraded is False
