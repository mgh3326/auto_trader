import datetime as dt

import pytest

from app.schemas.investment_snapshots import SnapshotCreate, SnapshotRunCreate
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_NOW = dt.datetime(2026, 5, 30, 9, 0, tzinfo=dt.timezone.utc)


async def _new_run(repo: InvestmentSnapshotsRepository):
    # Use SnapshotRunCreate payload object — matches the real insert_run signature.
    return await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            requested_by="claude_code",
        )
    )


def _snap(run_uuid, kind: str, scope: str | None) -> SnapshotCreate:
    return SnapshotCreate(
        run_uuid=run_uuid,
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",
        account_scope=scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json={"k": kind, "fixed": "payload"},
        as_of=_NOW,
        freshness_status="fresh",
    )


@pytest.mark.asyncio
async def test_market_snapshot_scope_is_normalized_to_none(db_session) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    row = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_live"))
    assert row.account_scope is None


@pytest.mark.asyncio
async def test_portfolio_snapshot_scope_is_preserved(db_session) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    row = await repo.insert_snapshot(_snap(run.run_uuid, "portfolio", "kis_live"))
    assert row.account_scope == "kis_live"


@pytest.mark.asyncio
async def test_same_market_payload_dedups_across_live_and_mock(db_session) -> None:
    """kis_live and kis_mock requests for identical market payload share ONE row."""
    repo = InvestmentSnapshotsRepository(db_session)
    run = await _new_run(repo)
    live = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_live"))
    mock = await repo.insert_snapshot(_snap(run.run_uuid, "market", "kis_mock"))
    assert live.snapshot_uuid == mock.snapshot_uuid
    assert mock.account_scope is None
