import datetime as dt
from decimal import Decimal
import uuid

import pytest

from app.models.analysis import StockInfo
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from app.models.investment_stages import InvestmentStageRun
from app.services.investment_stages.hermes_context import HermesContextExporter
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.schemas.investment_snapshots import (
    SnapshotRunCreate,
    SnapshotCreate,
    BundleCreate,
    BundleItemCreate,
)


async def _clear(db_session):
    from sqlalchemy import text
    await db_session.execute(text("DELETE FROM market_valuation_snapshots"))
    await db_session.execute(text("DELETE FROM stock_info WHERE symbol = 'AAPL'"))
    await db_session.commit()


@pytest.mark.asyncio
async def test_exporter_attaches_fundamentals_evidence_bundle(db_session) -> None:
    await _clear(db_session)

    # 1. Seed InvestmentSnapshotBundle + Snapshot + BundleItem (holding AAPL)
    snapshots_repo = InvestmentSnapshotsRepository(db_session)
    now = dt.datetime.now(tz=dt.UTC)
    
    run_obj = await snapshots_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="us",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    
    portfolio_snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run_obj.run_uuid,
            snapshot_kind="portfolio",
            market="us",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={
                "primary_source": "kis",
                "holdings": [
                    {
                        "ticker": "AAPL",
                        "quantity": 10,
                        "sellable_quantity": 10,
                        "source": "kis",
                        "market": "us",
                    }
                ],
                "reference_holdings": [],
                "count": 1,
                "market": "us",
            },
            as_of=now,
            freshness_status="fresh",
        )
    )
    
    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose=f"fundamentals_test_{uuid.uuid4().hex[:8]}",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    
    await snapshots_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=portfolio_snap.snapshot_uuid, role="required"),
    )

    # 2. Seed a MarketValuationSnapshot for AAPL
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol="AAPL",
            snapshot_date=now.date(),
            source="yahoo",
            per=Decimal("28.5"),
            pbr=Decimal("45"),
            roe=Decimal("1.5"),
            dividend_yield=Decimal("0.005"),
            market_cap=Decimal("3000000000000"),
            high_52w=Decimal("260"),
            low_52w=Decimal("164"),
        )
    )

    # 3. Seed StockInfo for AAPL
    db_session.add(
        StockInfo(
            symbol="AAPL",
            name="Apple Inc.",
            instrument_type="equity_us",
            sector="Technology",
            is_active=True,
        )
    )
    await db_session.commit()

    # 4. Seed an InvestmentStageRun associated with the bundle
    stage_run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="us",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=now,
    )
    db_session.add(stage_run)
    await db_session.commit()

    # 5. Export Hermes Context
    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert "fundamentals" in payload.dimension_evidence
    fund = payload.dimension_evidence["fundamentals"]
    assert fund["market"] == "us"
    assert fund["data_health"]["requested"] >= 1
    assert fund["data_health"]["covered"] >= 1
    assert fund["covered_count"] >= 1
    
    aapl_row = next(r for r in fund["per_symbol"] if r["symbol"] == "AAPL")
    assert aapl_row["sector"] == "Technology"
    assert aapl_row["per"] == 28.5
    assert aapl_row["dividend_yield"] == 0.005
    assert fund["freshness"]["status"] == "fresh"

