from __future__ import annotations

import datetime as dt
from unittest.mock import AsyncMock, MagicMock

import pytest
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.services.invest_screener_snapshots.partition_health import (
    active_universe_count,
    cap_degraded,
    resolve_healthy_partition,
)

_TEST_DATES = {
    dt.date(2040, 5, 1),
    dt.date(2040, 5, 19),
    dt.date(2040, 5, 20),
    dt.date(2040, 5, 22),
}


async def _cleanup(session) -> None:
    await session.execute(
        sa.delete(InvestScreenerSnapshot).where(
            InvestScreenerSnapshot.snapshot_date.in_(_TEST_DATES)
        )
    )
    await session.flush()


def test_cap_degraded_floors_fresh_and_partial_to_stale():
    assert cap_degraded("fresh") == "stale"
    assert cap_degraded("partial") == "stale"
    assert cap_degraded("stale") == "stale"
    assert cap_degraded("missing") == "missing"
    assert cap_degraded("fallback") == "fallback"


def _snap(symbol: str, snapshot_date: dt.date) -> InvestScreenerSnapshot:
    return InvestScreenerSnapshot(
        market="kr",
        symbol=symbol,
        snapshot_date=snapshot_date,
        consecutive_up_days=5,
        week_change_rate=1.0,
        change_rate=1.0,
        closes_window=[1, 2, 3, 4, 5],
        computed_at=dt.datetime(2040, 5, 22, 0, 30, tzinfo=dt.UTC),
        source="kis",
        latest_close=10000.0,
    )


async def _seed(session, *, date_counts: dict[dt.date, int]) -> None:
    n = 0
    for d, cnt in date_counts.items():
        for _ in range(cnt):
            n += 1
            session.add(_snap(f"{n:06d}", d))
    await session.flush()


_KW = {
    "model": InvestScreenerSnapshot,
    "date_col": InvestScreenerSnapshot.snapshot_date,
    "market_col": InvestScreenerSnapshot.market,
    "market": "kr",
}


@pytest.mark.asyncio
async def test_resolve_latest_healthy(db_session):
    await _cleanup(db_session)
    d = dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={d: 60})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == d
    assert hp.healthy is True and hp.is_fallback is False


@pytest.mark.asyncio
async def test_resolve_thin_latest_falls_back_to_older_healthy(db_session):
    await _cleanup(db_session)
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={older: 60, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == older
    assert hp.healthy is True and hp.is_fallback is True


@pytest.mark.asyncio
async def test_resolve_all_thin_serves_newest_as_last_resort(db_session):
    await _cleanup(db_session)
    older, newer = dt.date(2040, 5, 19), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={older: 3, newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=100, **_KW)
    assert hp is not None and hp.partition_date == newer  # NOT None
    assert hp.healthy is False and hp.is_fallback is False
    assert hp.row_count == 5


@pytest.mark.asyncio
async def test_resolve_empty_table_returns_none():
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_session.execute.return_value = mock_result

    hp = await resolve_healthy_partition(mock_session, universe_count=100, **_KW)
    assert hp is None


@pytest.mark.asyncio
async def test_resolve_universe_zero_disables_gate(db_session):
    await _cleanup(db_session)
    newer = dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={newer: 5})
    hp = await resolve_healthy_partition(db_session, universe_count=0, **_KW)
    assert hp is not None and hp.partition_date == newer
    assert hp.healthy is True


@pytest.mark.asyncio
async def test_resolve_scan_back_bound_does_not_reach_distant_healthy(db_session):
    await _cleanup(db_session)
    # Newest 2 are thin; a healthy partition exists but beyond max_scan_back=2.
    healthy_far = dt.date(2040, 5, 1)
    thin1, thin2 = dt.date(2040, 5, 20), dt.date(2040, 5, 22)
    await _seed(db_session, date_counts={healthy_far: 60, thin1: 5, thin2: 5})
    hp = await resolve_healthy_partition(
        db_session, universe_count=100, max_scan_back=2, **_KW
    )
    assert hp is not None and hp.partition_date == thin2  # last resort, not healthy_far
    assert hp.healthy is False


@pytest.mark.asyncio
async def test_active_universe_count_counts_active_kr(db_session):
    from app.models.kr_symbol_universe import KRSymbolUniverse

    initial_count = await active_universe_count(db_session, market="kr")

    symbol_1 = "999901"
    symbol_2 = "999902"

    # Cleanup these symbols in case they exist from aborted runs
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_({symbol_1, symbol_2})
        )
    )
    await db_session.flush()

    db_session.add(
        KRSymbolUniverse(symbol=symbol_1, name="A", exchange="KRX", is_active=True)
    )
    db_session.add(
        KRSymbolUniverse(symbol=symbol_2, name="B", exchange="KRX", is_active=False)
    )
    await db_session.flush()

    assert await active_universe_count(db_session, market="kr") == initial_count + 1

    # Cleanup after test
    await db_session.execute(
        sa.delete(KRSymbolUniverse).where(
            KRSymbolUniverse.symbol.in_({symbol_1, symbol_2})
        )
    )
    await db_session.flush()
