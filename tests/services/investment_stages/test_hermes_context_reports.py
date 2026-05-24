import datetime as dt
import uuid

import pytest

from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageRun
from app.models.investment_symbol_intermediate_reports import (
    InvestmentSymbolIntermediateReport,
)
from app.services.investment_stages.hermes_context import HermesContextExporter


@pytest.mark.asyncio
async def test_exporter_includes_dimension_and_symbol_reports(db_session) -> None:
    # 1. Seed bundle
    bundle = InvestmentSnapshotBundle(
        bundle_uuid=uuid.uuid4(),
        purpose="report_generation",
        market="us",
        account_scope=None,
        policy_version="intraday_action_report_v1",
        as_of=dt.datetime.now(tz=dt.UTC),
        status="complete",
        coverage_summary={},
        freshness_summary={},
        idempotency_key=str(uuid.uuid4()),
    )
    db_session.add(bundle)
    await db_session.commit()

    # 2. Seed Stage Run
    run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="us",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=dt.datetime.now(tz=dt.UTC),
    )
    db_session.add(run)
    await db_session.commit()

    # 3. Seed one Dimension Report
    d_report = InvestmentDimensionReport(
        run_uuid=run.run_uuid,
        snapshot_bundle_uuid=bundle.bundle_uuid,
        dimension="market",
        market="us",
        symbol=None,
        artifact_version=1,
        stance="bullish",
        confidence=80,
        report_text="Market is extremely strong.",
        key_findings=["Breadth is strong"],
        content_hash="h1",
        idempotency_key=f"{run.run_uuid}:market:us::h1",
    )
    db_session.add(d_report)

    # 4. Seed one Symbol Intermediate Report
    s_report = InvestmentSymbolIntermediateReport(
        run_uuid=run.run_uuid,
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="us",
        symbol="005930",
        decision_bucket="new_buy_candidate",
        verdict="buy",
        confidence=90,
        summary="Extremely positive signals",
        content_hash="h2",
        idempotency_key=f"{run.run_uuid}:005930:final_report_symbol:h2",
    )
    db_session.add(s_report)
    await db_session.commit()

    # 5. Export context
    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    # 6. Assert
    assert any(d["dimension"] == "market" for d in payload.dimension_reports)
    assert payload.dimension_reports[0]["stance"] == "bullish"
    assert payload.dimension_reports[0]["report_text"] == "Market is extremely strong."

    assert any(s["symbol"] == "005930" for s in payload.symbol_intermediate_reports)
    assert (
        payload.symbol_intermediate_reports[0]["decision_bucket"] == "new_buy_candidate"
    )
