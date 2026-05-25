import datetime as dt
import uuid
from decimal import Decimal

import pytest

from app.models.investment_snapshots import InvestmentSnapshotBundle
from app.models.investment_stages import InvestmentStageRun
from app.services.invest_screener_snapshots.repository import (
    InvestScreenerSnapshotsRepository,
    SnapshotUpsert,
)
from app.services.investment_stages.hermes_context import HermesContextExporter


@pytest.mark.asyncio
async def test_exporter_attaches_market_evidence_bundle(db_session) -> None:
    # Seed a bundle
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

    # Seed some screener snapshots
    repo = InvestScreenerSnapshotsRepository(db_session)
    base = {"market": "us", "snapshot_date": dt.date(2026, 5, 23), "source": "yahoo"}
    await repo.upsert(
        SnapshotUpsert(
            symbol="AAA",
            latest_close=Decimal("10"),
            change_rate=Decimal("5.0"),
            closes_window=[10],
            consecutive_up_days=3,
            **base,
        )
    )
    await repo.upsert(
        SnapshotUpsert(
            symbol="BBB",
            latest_close=Decimal("10"),
            change_rate=Decimal("-2.0"),
            closes_window=[10],
            **base,
        )
    )
    await db_session.commit()

    # Seed an investment stage run associated with the bundle
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

    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert "market" in payload.dimension_evidence
    market_ev = payload.dimension_evidence["market"]
    assert market_ev["market"] == "us"
    assert market_ev["breadth"]["advancers"] >= 1
    assert market_ev["breadth"]["decliners"] >= 1
    assert market_ev["top_movers"][0]["symbol"] == "AAA"
