import datetime as dt
from decimal import Decimal

import pytest

from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
)


async def _clear(db_session):
    from sqlalchemy import text

    await db_session.execute(text("DELETE FROM market_valuation_snapshots"))
    await db_session.commit()


def _row(symbol, *, snapshot_date, per, source="yahoo", market="us"):
    return MarketValuationSnapshot(
        market=market,
        symbol=symbol,
        snapshot_date=snapshot_date,
        source=source,
        per=Decimal(per),
        pbr=Decimal("1.2"),
        roe=Decimal("0.15"),
        dividend_yield=Decimal("0.02"),
        market_cap=Decimal("1000000"),
        high_52w=Decimal("200"),
        low_52w=Decimal("100"),
    )


@pytest.mark.asyncio
async def test_latest_for_symbols_returns_newest_per_symbol(db_session):
    await _clear(db_session)
    db_session.add_all(
        [
            _row("AAPL", snapshot_date=dt.date(2026, 5, 20), per="10"),
            _row("AAPL", snapshot_date=dt.date(2026, 5, 23), per="12"),  # newest
            _row("MSFT", snapshot_date=dt.date(2026, 5, 23), per="30"),
        ]
    )
    await db_session.commit()

    repo = MarketValuationSnapshotsRepository(db_session)
    rows = await repo.latest_for_symbols(market="us", symbols={"AAPL", "MSFT", "TSLA"})
    by_symbol = {r.symbol: r for r in rows}
    assert set(by_symbol) == {"AAPL", "MSFT"}  # TSLA absent
    assert by_symbol["AAPL"].per == Decimal("12")  # newest snapshot_date


@pytest.mark.asyncio
async def test_latest_for_symbols_empty_input(db_session):
    repo = MarketValuationSnapshotsRepository(db_session)
    assert await repo.latest_for_symbols(market="us", symbols=set()) == []
