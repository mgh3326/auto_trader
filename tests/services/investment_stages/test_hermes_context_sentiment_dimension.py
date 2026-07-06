import datetime as dt
import uuid

import pytest

from app.models.analysis import StockInfo
from app.models.investment_stages import InvestmentStageRun
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.services.investment_stages.hermes_context import HermesContextExporter


async def _clear(db_session, symbol: str) -> None:
    from sqlalchemy import text

    await db_session.execute(
        text("DELETE FROM investor_flow_snapshots WHERE symbol = :symbol"),
        {"symbol": symbol},
    )
    await db_session.execute(
        text("DELETE FROM stock_info WHERE symbol = :symbol"),
        {"symbol": symbol},
    )
    await db_session.commit()


@pytest.mark.asyncio
async def test_exporter_attaches_sentiment_evidence_bundle_kr(db_session) -> None:
    symbol = f"TST{uuid.uuid4().hex[:8].upper()}"
    await _clear(db_session, symbol)

    # 1. Seed InvestmentSnapshotBundle + Snapshot + BundleItem (holding symbol)
    snapshots_repo = InvestmentSnapshotsRepository(db_session)
    now = dt.datetime.now(tz=dt.UTC)

    run_obj = await snapshots_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )

    portfolio_snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run_obj.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={
                "primary_source": "kis",
                "holdings": [
                    {
                        "ticker": symbol,
                        "quantity": 10,
                        "sellable_quantity": 10,
                        "source": "kis",
                        "market": "kr",
                    }
                ],
                "reference_holdings": [],
                "count": 1,
                "market": "kr",
            },
            as_of=now,
            freshness_status="fresh",
        )
    )

    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose=f"sentiment_test_{uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )

    await snapshots_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(
            snapshot_uuid=portfolio_snap.snapshot_uuid, role="required"
        ),
    )

    # 2. Seed an InvestorFlowSnapshot for the held symbol
    db_session.add(
        InvestorFlowSnapshot(
            market="kr",
            symbol=symbol,
            snapshot_date=now.date(),
            foreign_net=120000,
            institution_net=5000,
            double_buy=True,
            double_sell=False,
            foreign_consecutive_buy_days=3,
            institution_consecutive_buy_days=2,
            source="naver_finance",
        )
    )

    # 3. Seed StockInfo for the held symbol
    db_session.add(
        StockInfo(
            symbol=symbol,
            name="Sentiment Test Equity",
            instrument_type="equity_kr",
            sector="Technology",
            is_active=True,
        )
    )
    await db_session.commit()

    # 4. Seed an InvestmentStageRun associated with the bundle
    stage_run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="kr",
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

    assert "sentiment" in payload.dimension_evidence
    sent = payload.dimension_evidence["sentiment"]
    assert sent["market"] == "kr"
    assert sent["data_health"]["requested"] >= 1
    assert sent["data_health"]["covered"] >= 1
    assert sent["covered_count"] >= 1

    symbol_row = next(r for r in sent["per_symbol"] if r["symbol"] == symbol)
    assert symbol_row["foreign_net"] == 120000
    assert symbol_row["double_buy"] is True
    assert symbol_row["foreign_consecutive_buy_days"] == 3
    assert sent["freshness"]["status"] == "fresh"


@pytest.mark.asyncio
async def test_exporter_attaches_sentiment_evidence_bundle_us_unavailable(
    db_session,
) -> None:
    symbol = f"TUS{uuid.uuid4().hex[:8].upper()}"
    await _clear(db_session, symbol)

    # 1. Seed InvestmentSnapshotBundle + Snapshot + BundleItem (holding symbol)
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
                        "ticker": symbol,
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
            purpose=f"sentiment_us_test_{uuid.uuid4().hex[:8]}",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )

    await snapshots_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(
            snapshot_uuid=portfolio_snap.snapshot_uuid, role="required"
        ),
    )
    await db_session.commit()

    # 2. Seed an InvestmentStageRun associated with the bundle
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

    # 3. Export Hermes Context
    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert "sentiment" in payload.dimension_evidence
    sent = payload.dimension_evidence["sentiment"]
    assert sent["market"] == "us"
    assert sent["freshness"]["status"] == "unavailable"
    assert sent["per_symbol"] == []


@pytest.mark.asyncio
async def test_exporter_crypto_bundle_gets_dimensions_sentiment_unavailable(
    db_session,
) -> None:
    """ROB-369 E11 — crypto bundles previously got ``dimension_evidence={}``
    (hard kr/us gate). The gate now includes crypto, so the same per-dimension
    synthesis runs: all four dimension keys are present, and sentiment is
    honestly ``unavailable`` (investor-flow is KR-only) — never fabricated or
    KR-leaking."""
    symbol = f"TCR{uuid.uuid4().hex[:8].upper()}"
    await _clear(db_session, symbol)
    snapshots_repo = InvestmentSnapshotsRepository(db_session)
    now = dt.datetime.now(tz=dt.UTC)

    run_obj = await snapshots_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="crypto",
            account_scope="upbit_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    portfolio_snap = await snapshots_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run_obj.run_uuid,
            snapshot_kind="portfolio",
            market="crypto",
            account_scope="upbit_live",
            source_kind="auto_trader_mcp",
            payload_json={
                "primary_source": "upbit",
                "holdings": [{"ticker": symbol, "source": "upbit", "market": "CRYPTO"}],
                "reference_holdings": [],
                "count": 1,
                "market": "crypto",
            },
            as_of=now,
            freshness_status="fresh",
        )
    )
    bundle = await snapshots_repo.insert_bundle(
        BundleCreate(
            purpose=f"crypto_dim_test_{uuid.uuid4().hex[:8]}",
            market="crypto",
            account_scope="upbit_live",
            policy_version="intraday_action_report_v1",
            as_of=now,
            status="complete",
        )
    )
    await snapshots_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(
            snapshot_uuid=portfolio_snap.snapshot_uuid, role="required"
        ),
    )
    await db_session.commit()

    stage_run = InvestmentStageRun(
        run_uuid=uuid.uuid4(),
        snapshot_bundle_uuid=bundle.bundle_uuid,
        market="crypto",
        account_scope=None,
        policy_version="v1",
        generator_version="v1",
        status="running",
        started_at=now,
    )
    db_session.add(stage_run)
    await db_session.commit()

    exporter = HermesContextExporter(db_session, stages=[])
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    de = payload.dimension_evidence
    assert de != {}, "crypto dimension_evidence must no longer be silently empty"
    # All four dimensions are now synthesized for crypto (gate lifted).
    assert {"market", "news", "fundamentals", "sentiment"} <= set(de)
    # Sentiment is KR-only by design → explicit unavailable for crypto.
    assert de["sentiment"]["market"] == "crypto"
    assert de["sentiment"]["freshness"]["status"] == "unavailable"
    assert de["sentiment"]["per_symbol"] == []
