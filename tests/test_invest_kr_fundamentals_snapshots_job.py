from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.jobs.invest_kr_fundamentals_snapshots import (
    KrFundamentalsSnapshotBuildRequest,
    run_kr_fundamentals_snapshot_build,
)
from app.services.invest_kr_fundamentals_snapshots.builder import (
    KrFundamentalsProviderRow,
)

_JOB_SYMBOL = "990010"


class _FakeProvider:
    async def fetch_rows(
        self, *, limit: int | None = None
    ) -> list[KrFundamentalsProviderRow]:
        rows = [
            KrFundamentalsProviderRow(
                symbol=_JOB_SYMBOL,
                name="잡테스트",
                price=Decimal("12345"),
                roe_ttm=Decimal("10.0"),
            )
        ]
        return rows[:limit] if limit is not None else rows


@pytest.mark.asyncio
async def test_run_job_dry_run_persists_nothing(db_session):
    await db_session.execute(
        text("DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol = :s"),
        {"s": _JOB_SYMBOL},
    )
    await db_session.commit()

    with patch(
        "app.jobs.invest_kr_fundamentals_snapshots.TvScreenerKrFundamentalsProvider",
        _FakeProvider,
    ):
        result = await run_kr_fundamentals_snapshot_build(
            KrFundamentalsSnapshotBuildRequest(limit=5, commit=False)
        )

    assert result["committed"] is False
    assert result["would_upsert"] == 1
    assert result["upserted"] == 0

    persisted = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM invest_kr_fundamentals_snapshots "
                "WHERE symbol = :s"
            ),
            {"s": _JOB_SYMBOL},
        )
    ).scalar_one()
    assert persisted == 0
