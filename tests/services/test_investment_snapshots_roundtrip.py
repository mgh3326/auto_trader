# tests/services/test_investment_snapshots_roundtrip.py
import datetime as dt

import pytest
import sqlalchemy as sa

from app.models.investment_snapshots import (
    InvestmentSnapshot,
    InvestmentSnapshotBundleItem,
)
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
async def test_full_bundle_round_trip(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
            policy_snapshot_json={
                "portfolio": {"soft_ttl": 60, "hard_ttl": 300},
                "market": {"soft_ttl": 180, "hard_ttl": 600},
                "candidate_universe": {"soft_ttl": 900, "hard_ttl": 3600},
            },
        )
    )

    portfolio = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="kis_mcp",
            payload_json={"cash_krw": 1_000_000, "holdings": []},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    market = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table="market_quote_snapshots",
            source_id=1,
            source_uri="market_quote_snapshots:1",
            payload_json={"kospi": 2710.0},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    candidate = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="candidate_universe",
            market="kr",
            source_kind="domain_ref",
            source_table="invest_screener_snapshots",
            source_id=42,
            source_uri="invest_screener_snapshots:42",
            payload_json={"top_n": [{"symbol": "035420"}]},
            as_of=_now(),
            freshness_status="fresh",
        )
    )

    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose="kr_action_report_roundtrip",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
            coverage_summary={
                "required": {"portfolio": "fresh", "market": "fresh"},
                "optional": {"candidate_universe": "fresh"},
            },
            freshness_summary={
                "portfolio": {"status": "fresh"},
                "market": {"status": "fresh"},
                "candidate_universe": {"status": "fresh"},
            },
        )
    )

    for snap, role in [
        (portfolio, "required"),
        (market, "required"),
        (candidate, "optional"),
    ]:
        await repo.link_bundle_item(
            bundle_uuid=bundle.bundle_uuid,
            item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role=role),
        )
    await db_session.commit()

    # Read back: 3 items linked to this bundle.
    rows = (
        await db_session.execute(
            sa.select(InvestmentSnapshotBundleItem).where(
                InvestmentSnapshotBundleItem.bundle_id == bundle.id
            )
        )
    ).scalars().all()
    assert len(rows) == 3
    assert {r.role for r in rows} == {"required", "optional"}

    # source_ref triple persisted on domain_ref snapshots.
    market_row = await db_session.scalar(
        sa.select(InvestmentSnapshot).where(InvestmentSnapshot.id == market.id)
    )
    assert market_row.source_uri == "market_quote_snapshots:1"
