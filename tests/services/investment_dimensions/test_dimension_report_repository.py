import datetime as dt
import uuid

import pytest

from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.models.investment_stages import InvestmentStageRun
from app.services.investment_dimensions.dimension_report_repository import (
    DimensionReportRepository,
)


async def _seed_run(db_session) -> InvestmentStageRun:
    run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=uuid.uuid4(),
        market="us",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(run)
    await db_session.commit()
    return run


async def _add(db_session, run_uuid, *, content_hash, dimension="market"):
    row = InvestmentDimensionReport(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=uuid.uuid4(),
        dimension=dimension,
        market="us",
        symbol=None,
        artifact_version=1,
        report_text="x",
        stance="bullish",
        confidence=70,
        content_hash=content_hash,
        idempotency_key=f"{run_uuid}:{dimension}:us::{content_hash}",
    )
    db_session.add(row)
    await db_session.commit()
    return row


@pytest.mark.asyncio
async def test_list_for_run_and_get_by_uuids(db_session):
    repo = DimensionReportRepository(db_session)
    run = await _seed_run(db_session)
    r1 = await _add(db_session, run.run_uuid, content_hash="h1")
    listed = await repo.list_for_run(run.run_uuid)
    assert [r.dimension_report_uuid for r in listed] == [r1.dimension_report_uuid]
    got = await repo.get_by_uuids([r1.dimension_report_uuid, uuid.uuid4()])
    assert [r.dimension_report_uuid for r in got] == [r1.dimension_report_uuid]
