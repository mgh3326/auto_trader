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
    # ROB-429 A2: dry-run still carries the coverage guard metadata.
    assert "active_universe_count" in result
    assert "coverage_ratio" in result
    assert "commit_allowed" in result
    assert "block_reason" in result


@pytest.mark.asyncio
async def test_run_job_commit_blocked_below_floor_persists_nothing(db_session):
    """ROB-429 A2: a thin --commit (below 80% of universe) is guard-blocked and
    persists nothing, even though commit was requested."""
    await db_session.execute(
        text("DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol = :s"),
        {"s": _JOB_SYMBOL},
    )
    await db_session.commit()

    async def _fake_universe(session, *, market):
        return 100  # floor = ceil(0.80 * 100) = 80; the fake provider yields 1 row

    with (
        patch(
            "app.jobs.invest_kr_fundamentals_snapshots.TvScreenerKrFundamentalsProvider",
            _FakeProvider,
        ),
        patch(
            "app.jobs.invest_kr_fundamentals_snapshots.active_universe_count",
            _fake_universe,
        ),
    ):
        result = await run_kr_fundamentals_snapshot_build(
            KrFundamentalsSnapshotBuildRequest(limit=5, commit=True)
        )

    assert result["active_universe_count"] == 100
    assert result["commit_allowed"] is False
    assert result["committed"] is False
    assert result["upserted"] == 0
    assert result["block_reason"] is not None

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


@pytest.mark.asyncio
async def test_run_job_commit_allow_partial_persists(db_session):
    """ROB-429 A2: --allow-partial overrides the guard and commits the thin build."""
    await db_session.execute(
        text("DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol = :s"),
        {"s": _JOB_SYMBOL},
    )
    await db_session.commit()

    async def _fake_universe(session, *, market):
        return 100

    try:
        with (
            patch(
                "app.jobs.invest_kr_fundamentals_snapshots.TvScreenerKrFundamentalsProvider",
                _FakeProvider,
            ),
            patch(
                "app.jobs.invest_kr_fundamentals_snapshots.active_universe_count",
                _fake_universe,
            ),
        ):
            result = await run_kr_fundamentals_snapshot_build(
                KrFundamentalsSnapshotBuildRequest(
                    limit=5, commit=True, allow_partial=True
                )
            )

        assert result["committed"] is True
        assert result["commit_allowed"] is True
        assert result["upserted"] == 1

        persisted = (
            await db_session.execute(
                text(
                    "SELECT count(*) FROM invest_kr_fundamentals_snapshots "
                    "WHERE symbol = :s"
                ),
                {"s": _JOB_SYMBOL},
            )
        ).scalar_one()
        assert persisted == 1
    finally:
        await db_session.execute(
            text("DELETE FROM invest_kr_fundamentals_snapshots WHERE symbol = :s"),
            {"s": _JOB_SYMBOL},
        )
        await db_session.commit()
