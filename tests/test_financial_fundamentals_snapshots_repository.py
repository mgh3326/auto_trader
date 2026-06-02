from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.financial_fundamentals_snapshots.repository import (
    FinancialFundamentalsSnapshotsRepository,
    FinancialFundamentalsUpsert,
)


def _row(fiscal_period: str, net_income: int, *, filing_date: dt.date | None) -> FinancialFundamentalsUpsert:
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
