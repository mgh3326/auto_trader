import datetime as dt

import pytest

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
)


def _snap(symbol, change_rate, d):
    return InvestScreenerSnapshot(
        market="us",
        symbol=symbol,
        snapshot_date=d,
        latest_close=10,
        change_rate=change_rate,
        closes_window=[],
        source="yahoo",
        daily_volume=1_000_000,
    )


@pytest.mark.asyncio
async def test_list_candidate_pool_returns_wide_unlimited(db_session):
    today = dt.date(2026, 6, 9)
    db_session.add_all([_snap(f"S{i}", float(i), today) for i in range(30)])
    await db_session.flush()
    repo = InvestScreenerSnapshotsRepository(db_session)
    rows = await repo.list_candidate_pool(market="us", limit=None)
    assert len(rows) == 30  # no early cap


@pytest.mark.asyncio
async def test_common_stock_flags_lookup(db_session):
    db_session.add_all(
        [
            USSymbolUniverse(
                symbol="AAA", exchange="NASDAQ", is_active=True, is_common_stock=True
            ),
            USSymbolUniverse(
                symbol="ETF1", exchange="NYSE", is_active=True, is_common_stock=False
            ),
        ]
    )
    await db_session.flush()
    repo = InvestScreenerSnapshotsRepository(db_session)
    flags = await repo.common_stock_flags(["AAA", "ETF1", "MISSING"])
    assert flags["AAA"] is True
    assert flags["ETF1"] is False
    assert flags.get("MISSING") is None
