"""AnalystConsensusSnapshotsRepository upsert → coverage → existing_keys roundtrip (ROB-641)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.analyst_consensus_snapshot import AnalystConsensusSnapshot
from app.services.analyst_consensus_snapshots.repository import (
    AnalystConsensusSnapshotsRepository,
    AnalystConsensusSnapshotUpsert,
)

_UNIQUE = 0


def _unique_symbol() -> str:
    global _UNIQUE
    _UNIQUE += 1
    return f"T641S{_UNIQUE:04d}"


def _row(
    *,
    symbol: str,
    snapshot_date: dt.date = dt.date(2026, 7, 2),
    source: str = "naver_finance",
    buy_count: int = 10,
    target_mean: Decimal = Decimal("100000.0000"),
) -> AnalystConsensusSnapshotUpsert:
    return AnalystConsensusSnapshotUpsert(
        market="kr",
        symbol=symbol,
        source=source,
        snapshot_date=snapshot_date,
        buy_count=buy_count,
        hold_count=5,
        sell_count=2,
        strong_buy_count=3,
        total_count=17,
        target_mean=target_mean,
        target_median=Decimal("95000.0000"),
        target_high=Decimal("120000.0000"),
        target_low=Decimal("80000.0000"),
        upside_pct=Decimal("15.5000"),
        analyst_count=17,
        newest_opinion_date=dt.date(2026, 6, 30),
        current_price=Decimal("86000.0000"),
        raw_payload={"consensus": {"buy_count": buy_count}},
    )


async def _cleanup(db_session, symbols: list[str]) -> None:
    await db_session.execute(
        sa.delete(AnalystConsensusSnapshot).where(
            AnalystConsensusSnapshot.symbol.in_(symbols)
        )
    )
    await db_session.commit()


async def _cleanup_all_test_rows(db_session) -> None:
    """Remove ALL test-prefix rows so market-wide coverage_counts is clean."""
    await db_session.execute(
        sa.delete(AnalystConsensusSnapshot).where(
            AnalystConsensusSnapshot.symbol.like("T641%")
        )
    )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_unique_key(db_session) -> None:
    symbol = _unique_symbol()
    await _cleanup(db_session, [symbol])

    repo = AnalystConsensusSnapshotsRepository(db_session)

    n = await repo.upsert([_row(symbol=symbol, buy_count=10)])
    await db_session.commit()
    assert n == 1

    # Same key → UPDATE not duplicate INSERT.
    await repo.upsert([_row(symbol=symbol, buy_count=99)])
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                sa.select(AnalystConsensusSnapshot).where(
                    AnalystConsensusSnapshot.symbol == symbol
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].buy_count == 99


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_dedupes_duplicate_keys_in_single_call(db_session) -> None:
    """Same conflict key twice in one payload must not crash Postgres.

    Without pre-statement dedupe a multi-row INSERT ... ON CONFLICT raises
    "ON CONFLICT DO UPDATE command cannot affect row a second time".
    Last occurrence wins.
    """
    symbol = _unique_symbol()
    await _cleanup(db_session, [symbol])

    repo = AnalystConsensusSnapshotsRepository(db_session)
    n = await repo.upsert(
        [
            _row(symbol=symbol, buy_count=10),
            _row(symbol=symbol, buy_count=42),
        ]
    )
    await db_session.commit()
    assert n == 1

    rows = (
        (
            await db_session.execute(
                sa.select(AnalystConsensusSnapshot).where(
                    AnalystConsensusSnapshot.symbol == symbol
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].buy_count == 42


@pytest.mark.integration
@pytest.mark.asyncio
async def test_coverage_counts_fresh_vs_stale(db_session) -> None:
    sym_a = _unique_symbol()
    sym_b = _unique_symbol()
    await _cleanup_all_test_rows(db_session)

    repo = AnalystConsensusSnapshotsRepository(db_session)
    fresh_date = dt.date(2026, 7, 2)
    stale_date = dt.date(2026, 6, 1)
    await repo.upsert(
        [
            _row(symbol=sym_a, snapshot_date=fresh_date),
            _row(symbol=sym_b, snapshot_date=stale_date),
        ]
    )
    await db_session.commit()

    counts = await repo.coverage_counts("kr", fresh_after=dt.date(2026, 6, 15))
    assert counts.fresh_symbols == 1  # sym_a (2026-07-02 >= cutoff)
    assert counts.stale_symbols == 1  # sym_b (2026-06-01 < cutoff)
    assert counts.latest_snapshot_date == fresh_date
    assert counts.total_symbols == 2


@pytest.mark.integration
@pytest.mark.asyncio
async def test_existing_keys_pre_flight(db_session) -> None:
    sym_a = _unique_symbol()
    sym_b = _unique_symbol()
    sym_c = _unique_symbol()
    await _cleanup(db_session, [sym_a, sym_b, sym_c])

    repo = AnalystConsensusSnapshotsRepository(db_session)
    snapshot_date = dt.date(2026, 7, 2)
    await repo.upsert([_row(symbol=sym_a, snapshot_date=snapshot_date)])
    await db_session.commit()

    # sym_a exists, sym_b and sym_c do not.
    existing = await repo.existing_keys(
        [
            _row(symbol=sym_a, snapshot_date=snapshot_date),
            _row(symbol=sym_b, snapshot_date=snapshot_date),
            _row(symbol=sym_c, snapshot_date=snapshot_date),
        ]
    )
    assert len(existing) == 1
    key = next(iter(existing))
    assert key[0] == "kr"
    assert key[1] == sym_a
    assert key[2] == snapshot_date
    assert key[3] == "naver_finance"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_different_sources_coexist(db_session) -> None:
    symbol = _unique_symbol()
    await _cleanup(db_session, [symbol])

    repo = AnalystConsensusSnapshotsRepository(db_session)
    snapshot_date = dt.date(2026, 7, 2)
    await repo.upsert(
        [
            _row(symbol=symbol, snapshot_date=snapshot_date, source="naver_finance"),
            _row(symbol=symbol, snapshot_date=snapshot_date, source="yfinance"),
        ]
    )
    await db_session.commit()

    rows = (
        (
            await db_session.execute(
                sa.select(AnalystConsensusSnapshot).where(
                    AnalystConsensusSnapshot.symbol == symbol
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    sources = {r.source for r in rows}
    assert sources == {"naver_finance", "yfinance"}
