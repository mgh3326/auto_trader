from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)


def _row(
    fiscal_period: str, net_income: int, *, filing_date: dt.date | None
) -> FinancialFundamentalsUpsert:
    return FinancialFundamentalsUpsert(
        market="kr",
        symbol="005930",
        fiscal_period=fiscal_period,
        period_type="annual",
        period_end_date=dt.date(int(fiscal_period[:4]), 12, 31),
        filing_date=filing_date,
        effective_at=filing_date,
        source="dart",
        source_collected_at=dt.datetime(2026, 6, 2, 0, 0, tzinfo=dt.UTC),
        revenue=Decimal("3000000"),
        net_income=Decimal(net_income),
        data_state="fresh" if filing_date else "partial",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_is_idempotent_on_unique_key(db_session):
    # Clean up first to avoid cross-test pollution in persistent test DB
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol == "005930"
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)

    n = await repo.upsert([_row("2025A", 100, filing_date=dt.date(2026, 3, 20))])
    await db_session.commit()
    assert n == 1

    # Same (market,symbol,fiscal_period,source) → UPDATE not duplicate INSERT.
    await repo.upsert([_row("2025A", 250, filing_date=dt.date(2026, 3, 20))])
    await db_session.commit()

    rows = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert len(rows) == 1
    assert rows[0].net_income == Decimal("250")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_periods_for_symbol_returns_ascending_by_period_end(db_session):
    # Clean up first to avoid cross-test pollution in persistent test DB
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol == "005930"
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        [
            _row("2023A", 100, filing_date=dt.date(2024, 3, 20)),
            _row("2025A", 300, filing_date=dt.date(2026, 3, 20)),
            _row("2024A", 200, filing_date=dt.date(2025, 3, 20)),
        ]
    )
    await db_session.commit()

    rows = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert [r.fiscal_period for r in rows] == ["2023A", "2024A", "2025A"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_dedupes_duplicate_keys_in_one_call_keeping_latest(db_session):
    # ROB-442: two rows with the same (market,symbol,fiscal_period,source) in a
    # single upsert() call must NOT raise "ON CONFLICT DO UPDATE command cannot
    # affect row a second time"; the latest-collected row wins deterministically.
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol == "005930"
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    base = _row("2025A", 100, filing_date=dt.date(2026, 3, 20))
    early = base.model_copy(
        update={
            "net_income": Decimal("100"),
            "source_collected_at": dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        }
    )
    late = base.model_copy(
        update={
            "net_income": Decimal("999"),
            "source_collected_at": dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
        }
    )

    await repo.upsert([early, late])
    await db_session.commit()

    rows = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert len(rows) == 1
    assert rows[0].net_income == Decimal("999")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_upsert_chunks_across_statements_on_real_db(db_session, monkeypatch):
    # ROB-442: force tiny chunks so >1 SQL statement is issued against a real
    # asyncpg backend, then prove every distinct row persists and re-running is
    # idempotent across chunk boundaries.
    from app.services.financial_fundamentals_snapshots import repository as repo_mod

    monkeypatch.setattr(repo_mod, "_MAX_BIND_PARAMS", 44)  # ~2 rows/chunk at 22 cols

    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol == "005930"
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    rows = [
        _row("2021A", 100, filing_date=dt.date(2022, 3, 20)),
        _row("2022A", 200, filing_date=dt.date(2023, 3, 20)),
        _row("2023A", 300, filing_date=dt.date(2024, 3, 20)),
        _row("2024A", 400, filing_date=dt.date(2025, 3, 20)),
        _row("2025A", 500, filing_date=dt.date(2026, 3, 20)),
    ]

    n = await repo.upsert(rows)
    await db_session.commit()

    # rowcount is summed across the (now 3) chunk statements on a real backend.
    assert n == 5

    persisted = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert [r.fiscal_period for r in persisted] == [
        "2021A",
        "2022A",
        "2023A",
        "2024A",
        "2025A",
    ]

    # idempotent re-run across chunk boundaries → still 5 rows, no duplicates
    await repo.upsert(rows)
    await db_session.commit()
    persisted_again = await repo.periods_for_symbol(market="kr", symbol="005930")
    assert len(persisted_again) == 5


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_periods_for_symbols_groups_by_symbol(db_session):
    # Clean up first to avoid cross-test pollution in persistent test DB
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(["005930", "000660"])
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        [
            _row("2023A", 100, filing_date=dt.date(2024, 3, 20)),
            _row("2024A", 200, filing_date=dt.date(2025, 3, 20)),
        ]
    )
    # a second symbol
    other = _row("2024A", 50, filing_date=dt.date(2025, 3, 20))
    other_dict = other.model_dump()
    other_dict["symbol"] = "000660"
    await repo.upsert([FinancialFundamentalsUpsert(**other_dict)])
    await db_session.commit()

    grouped = await repo.latest_periods_for_symbols(
        market="kr", symbols=["005930", "000660", "999999"]
    )
    assert set(grouped) == {"005930", "000660"}  # missing symbol absent, not error
    assert [r.fiscal_period for r in grouped["005930"]] == ["2023A", "2024A"]  # asc
    assert len(grouped["000660"]) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_latest_periods_for_symbols_does_not_load_raw_payload(db_session):
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(["005930", "000660"])
        )
    )
    await db_session.commit()

    repo = FinancialFundamentalsSnapshotsRepository(db_session)
    await repo.upsert(
        [
            _row("2023A", 100, filing_date=dt.date(2024, 3, 20)).model_copy(
                update={"raw_payload": {"large": "x" * 4096}}
            ),
            _row("2024A", 200, filing_date=dt.date(2025, 3, 20)).model_copy(
                update={"raw_payload": {"large": "y" * 4096}}
            ),
        ]
    )
    await db_session.commit()
    db_session.expunge_all()

    grouped = await repo.latest_periods_for_symbols(market="kr", symbols=["005930"])
    rows = grouped["005930"]

    assert [r.fiscal_period for r in rows] == ["2023A", "2024A"]
    assert rows[0].source_collected_at == dt.datetime(2026, 6, 2, 0, 0, tzinfo=dt.UTC)
    assert rows[0].revenue == Decimal("3000000")
    assert "raw_payload" in sa.inspect(rows[0]).unloaded
    assert "raw_payload" in sa.inspect(rows[1]).unloaded
