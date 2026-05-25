import datetime as dt
import uuid

import pytest

from app.models.investment_stages import InvestmentStageRun
from app.schemas.hermes_composition import HermesStageRunEnvelope
from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)
from app.services.investment_dimensions.dimension_report_ingest import (
    DimensionReportIngestService,
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


def _request(run, *, confidence, freshness_status):
    return HermesDimensionReportsIngestRequest(
        run_envelope=HermesStageRunEnvelope(
            run_uuid=run.run_uuid,
            snapshot_bundle_uuid=run.snapshot_bundle_uuid,
            market="us",
            account_scope=None,
            market_session=None,
        ),
        dimension_reports=[
            HermesDimensionReport(
                dimension="market",
                market="us",
                report_text="미국 시장 개요",
                stance="bullish",
                confidence=confidence,
                key_findings=["상승 우위 60%"],
                signals={"breadth": "60% adv"},
                freshness_summary={"status": freshness_status},
            )
        ],
    )


@pytest.mark.asyncio
async def test_ingest_persists_and_caps_confidence_by_freshness(db_session):
    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    resp = await svc.ingest_from_hermes(
        _request(run, confidence=90, freshness_status="stale")
    )
    await db_session.commit()
    rep = resp.results[0].report
    assert rep.dimension == "market"
    assert rep.symbol is None
    assert rep.stance == "bullish"
    assert rep.confidence == 40  # capped: stale → 40 (was 90)


@pytest.mark.asyncio
async def test_ingest_is_idempotent(db_session):
    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    req = _request(run, confidence=50, freshness_status="fresh")
    r1 = await svc.ingest_from_hermes(req)
    await db_session.commit()
    r2 = await svc.ingest_from_hermes(req)
    await db_session.commit()
    assert r2.results[0].idempotent_existing is True
    assert (
        r1.results[0].report.dimension_report_uuid
        == r2.results[0].report.dimension_report_uuid
    )


@pytest.mark.asyncio
async def test_ingest_rejects_unknown_run(db_session):
    from app.services.investment_dimensions.dimension_report_ingest import (
        DimensionReportIngestError,
    )

    run = await _seed_run(db_session)
    svc = DimensionReportIngestService(db_session)
    bad = _request(run, confidence=50, freshness_status="fresh")
    object.__setattr__(bad.run_envelope, "run_uuid", uuid.uuid4())
    with pytest.raises(DimensionReportIngestError):
        await svc.ingest_from_hermes(bad)
