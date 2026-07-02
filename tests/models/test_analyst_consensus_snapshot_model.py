"""AnalystConsensusSnapshot table contract (ROB-641)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.models.analyst_consensus_snapshot import AnalystConsensusSnapshot

_UNIQUE = 0


def _unique_symbol() -> str:
    """Generate a unique symbol per-test to avoid shared-DB collisions."""
    global _UNIQUE
    _UNIQUE += 1
    return f"T641M{_UNIQUE:04d}"


def _make_row(
    *, market: str = "kr", symbol: str | None = None
) -> AnalystConsensusSnapshot:
    return AnalystConsensusSnapshot(
        market=market,
        symbol=symbol or _unique_symbol(),
        source="naver_finance" if market == "kr" else "yfinance",
        snapshot_date=dt.date(2026, 7, 2),
        buy_count=5,
        hold_count=3,
        sell_count=1,
        strong_buy_count=2,
        total_count=9,
        target_mean=Decimal("85000.0000"),
        target_median=Decimal("82000.0000"),
        target_high=Decimal("95000.0000"),
        target_low=Decimal("70000.0000"),
        upside_pct=Decimal("12.5000"),
        analyst_count=9,
        newest_opinion_date=dt.date(2026, 6, 28),
        current_price=Decimal("76000.0000"),
        raw_payload={"consensus": {"buy_count": 5}},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_insert_and_read_back(db_session) -> None:
    symbol = _unique_symbol()
    db_session.add(_make_row(symbol=symbol))
    await db_session.flush()
    row = (
        await db_session.execute(
            sa.select(AnalystConsensusSnapshot).where(
                AnalystConsensusSnapshot.symbol == symbol
            )
        )
    ).scalar_one()
    assert row.market == "kr"
    assert row.source == "naver_finance"
    assert row.snapshot_date == dt.date(2026, 7, 2)
    assert row.buy_count == 5
    assert row.total_count == 9
    assert row.target_mean == Decimal("85000.0000")
    assert row.upside_pct == Decimal("12.5000")
    assert row.newest_opinion_date == dt.date(2026, 6, 28)
    assert row.current_price == Decimal("76000.0000")
    assert row.raw_payload["consensus"]["buy_count"] == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_duplicate_violates_unique(db_session) -> None:
    symbol = _unique_symbol()
    for _ in range(2):
        db_session.add(_make_row(symbol=symbol))
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_market(db_session) -> None:
    row = _make_row(market="crypto")
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_check_constraint_rejects_invalid_source(db_session) -> None:
    symbol = _unique_symbol()
    row = _make_row(symbol=symbol)
    row.source = "bloomberg"
    db_session.add(row)
    with pytest.raises(IntegrityError):
        await db_session.flush()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_nullable_counts_allowed(db_session) -> None:
    symbol = _unique_symbol()
    row = _make_row(symbol=symbol)
    row.buy_count = None
    row.hold_count = None
    row.sell_count = None
    row.total_count = None
    db_session.add(row)
    await db_session.flush()
    assert row.id is not None
