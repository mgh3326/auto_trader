from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.services.invest_view_model import screener_service


def _gainer(symbol: str, d: dt.date) -> InvestScreenerSnapshot:
    return InvestScreenerSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=d,
        consecutive_up_days=6,
        week_change_rate=3.5,
        change_rate=1.2,
        latest_close=80000,
        prev_close=79000,
        change_amount=1000,
        closes_window=[76000, 77000, 78000, 79000, 80000],
        daily_volume=1234567,
        computed_at=dt.datetime(2040, 5, 19, 0, 30, tzinfo=dt.UTC),
        source="kis",
    )


async def _seed_two_partitions(session, *, healthy_n: int, thin_n: int):
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)

    from app.models.kr_symbol_universe import KRSymbolUniverse
    # Clean up first
    await session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.snapshot_date.in_({older, newer})
        )
    )
    await session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like("99%"))
    )
    await session.flush()

    # Seed active universe (200 symbols)
    for i in range(200):
        sym = f"99{i:04d}"
        session.add(KRSymbolUniverse(symbol=sym, name=f"TestName{i}", exchange="KRX", is_active=True))
    await session.flush()

    # Seed snapshots
    for i in range(healthy_n):
        session.add(_gainer(f"99{i:04d}", older))
    for i in range(thin_n):
        session.add(_gainer(f"99{i:04d}", newer))
    await session.flush()

    return older, newer


@pytest.mark.asyncio
async def test_thin_newer_partition_does_not_shadow_healthy_older(db_session):
    # active universe = 200 + initial_active (say 18) = 218.
    # 50% bar is 109. healthy_n = 150 passes; thin_n = 20 fails.
    older, newer = await _seed_two_partitions(db_session, healthy_n=150, thin_n=20)
    rows = await screener_service.load_consecutive_gainers_from_snapshots(
        db_session, market="kr", limit=20
    )
    assert rows, "expected the healthy older partition to be served"
    # Every served row comes from the older healthy partition...
    assert all(r["snapshot_date"] == older for r in rows)
    # ...and is labeled stale (older than today), not fresh.
    assert all(r["_screener_snapshot_state"] == "stale" for r in rows)

    # Clean up after test
    from app.models.kr_symbol_universe import KRSymbolUniverse
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.snapshot_date.in_({older, newer})
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like("99%"))
    )
    await db_session.flush()



def _flow(symbol: str, d: dt.date) -> InvestorFlowSnapshot:
    return InvestorFlowSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=d,
        double_buy=True,
        foreign_consecutive_buy_days=5,
        foreign_net=1000,
        institution_net=10,
        individual_net=-10,
        collected_at=dt.datetime(2040, 5, 19, 0, 30, tzinfo=dt.UTC),
        source="kis",
    )


async def _seed_two_flow_partitions(session, *, healthy_n: int, thin_n: int):
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)

    from app.models.kr_symbol_universe import KRSymbolUniverse
    # Clean up first
    await session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.snapshot_date.in_({older, newer})
        )
    )
    await session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like("99%"))
    )
    await session.flush()

    # Seed active universe (200 symbols)
    for i in range(200):
        sym = f"99{i:04d}"
        session.add(KRSymbolUniverse(symbol=sym, name=f"TestName{i}", exchange="KRX", is_active=True))
    await session.flush()

    # Seed snapshots
    for i in range(healthy_n):
        session.add(_flow(f"99{i:04d}", older))
    for i in range(thin_n):
        session.add(_flow(f"99{i:04d}", newer))
    await session.flush()

    return older, newer


@pytest.mark.asyncio
async def test_investor_flow_thin_newer_falls_back_to_healthy_older(db_session):
    older, newer = await _seed_two_flow_partitions(db_session, healthy_n=150, thin_n=20)
    res = await screener_service._load_investor_flow_discovery_from_snapshots(
        db_session, market="kr", limit=20
    )
    assert res is not None
    rows = res.rows
    assert rows, "expected the healthy older investor_flow partition to be served"
    # Every served row comes from the older healthy partition...
    assert all(r["snapshot_date"] == older for r in rows)
    # ...and is labeled stale (older than today), not fresh.
    assert all(r["_screener_snapshot_state"] == "stale" for r in rows)

    # Clean up after test
    from app.models.kr_symbol_universe import KRSymbolUniverse
    await db_session.execute(
        sa.delete(InvestorFlowSnapshot).where(
            InvestorFlowSnapshot.snapshot_date.in_({older, newer})
        )
    )
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(KRSymbolUniverse.symbol.like("99%"))
    )
    await db_session.flush()
