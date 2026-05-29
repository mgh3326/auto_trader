"""ROB-366 B6 — Hermes context market_session derivation.

The exporter used to hardcode ``market_session=None``; it now derives it from
the latest InvestmentStageRun for the bundle (the run is where StageRunner
persists the session). DB-backed because the exporter skips run-loading under a
mock session.
"""

import datetime as dt
import uuid

import pytest

from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageRun
from app.services.investment_stages.hermes_context import HermesContextExporter


def _bundle() -> InvestmentSnapshotBundle:
    return InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="report_generation",
        market="us",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        as_of=dt.datetime.now(tz=dt.UTC),
        status="complete",
        coverage_summary={},
        freshness_summary={},
        idempotency_key=str(uuid.uuid4()),
    )


def _run(
    bundle_uuid: uuid.UUID,
    *,
    market_session: str | None,
    started_at: dt.datetime | None = None,
) -> InvestmentStageRun:
    return InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle_uuid,
        market="us",
        market_session=market_session,
        account_scope="kis_live",
        policy_version="v1",
        generator_version="v1",
        status="completed",
        started_at=started_at or dt.datetime.now(tz=dt.UTC),
    )


@pytest.mark.asyncio
async def test_market_session_derived_from_latest_run(db_session) -> None:
    bundle = _bundle()
    db_session.add(bundle)
    await db_session.commit()
    db_session.add(_run(bundle.bundle_uuid, market_session="regular"))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"


@pytest.mark.asyncio
async def test_market_session_none_when_no_run(db_session) -> None:
    bundle = _bundle()
    db_session.add(bundle)
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session is None


@pytest.mark.asyncio
async def test_market_session_uses_latest_run_when_multiple(db_session) -> None:
    # Bundle re-run across sessions: the most recent run (by started_at) wins.
    bundle = _bundle()
    db_session.add(bundle)
    await db_session.commit()
    now = dt.datetime.now(tz=dt.UTC)
    db_session.add(
        _run(
            bundle.bundle_uuid,
            market_session="pre",
            started_at=now - dt.timedelta(hours=2),
        )
    )
    db_session.add(_run(bundle.bundle_uuid, market_session="regular", started_at=now))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session == "regular"


@pytest.mark.asyncio
async def test_market_session_none_when_run_recorded_none(db_session) -> None:
    bundle = _bundle()
    db_session.add(bundle)
    await db_session.commit()
    db_session.add(_run(bundle.bundle_uuid, market_session=None))
    await db_session.commit()

    payload = await HermesContextExporter(db_session, stages=[]).export(
        snapshot_bundle_uuid=bundle.bundle_uuid
    )
    assert payload.market_session is None
