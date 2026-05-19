"""ROB-269 Phase 2 — SnapshotBundleReadService."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.schemas.investment_snapshots_mcp import (
    ListBundlesRequest,
    ListSnapshotsRequest,
)
from app.services.investment_snapshots.read_service import (
    SnapshotBundleNotFoundError,
    SnapshotBundleReadService,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


async def _seed_bundle_with_two_items(session) -> uuid.UUID:
    repo = InvestmentSnapshotsRepository(session)
    purpose = f"read_svc_{uuid.uuid4().hex[:8]}"
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap_a = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1_234_567, "u": str(uuid.uuid4())},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    snap_b = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table="market_quote_snapshots",
            source_id=42,
            source_uri=f"market_quote_snapshots:{uuid.uuid4().hex[:6]}",
            payload_json={"kospi": 2710.0, "u": str(uuid.uuid4())},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
        )
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap_a.snapshot_uuid, role="required"),
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap_b.snapshot_uuid, role="required"),
    )
    await session.commit()
    return bundle.bundle_uuid


@pytest.mark.asyncio
async def test_get_bundle_returns_404_for_unknown_uuid(db_session):
    svc = SnapshotBundleReadService(db_session)
    with pytest.raises(SnapshotBundleNotFoundError):
        await svc.get_bundle(bundle_uuid=uuid.uuid4())


@pytest.mark.asyncio
async def test_get_bundle_returns_header_and_items(db_session):
    bundle_uuid = await _seed_bundle_with_two_items(db_session)
    svc = SnapshotBundleReadService(db_session)
    response = await svc.get_bundle(bundle_uuid=bundle_uuid)

    assert response.bundle.bundle_uuid == bundle_uuid
    assert response.bundle.status == "complete"
    assert len(response.items) == 2
    assert {item.snapshot_kind for item in response.items} == {"portfolio", "market"}
    # By default no payload preview.
    assert response.payload_previews is None


@pytest.mark.asyncio
async def test_get_bundle_with_payload_preview_truncates(db_session):
    bundle_uuid = await _seed_bundle_with_two_items(db_session)
    svc = SnapshotBundleReadService(db_session)
    response = await svc.get_bundle(
        bundle_uuid=bundle_uuid, include_payload_preview=True
    )
    assert response.payload_previews is not None
    assert len(response.payload_previews) == 2
    for preview in response.payload_previews.values():
        # 2KB cap, leaves some room.
        assert len(preview.encode("utf-8")) <= 2049  # 2048 + ellipsis byte tolerance


@pytest.mark.asyncio
async def test_get_bundle_payload_preview_caps_oversized_payload(db_session):
    """Payload > 2KB → preview truncated with ellipsis."""
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    big_value = "x" * 5_000  # well over 2KB
    snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"big": big_value, "u": str(uuid.uuid4())},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose=f"big_preview_{uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
        )
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    await db_session.commit()

    svc = SnapshotBundleReadService(db_session)
    response = await svc.get_bundle(
        bundle_uuid=bundle.bundle_uuid, include_payload_preview=True
    )
    preview = next(iter(response.payload_previews.values()))
    assert preview.endswith("…")
    assert len(preview.encode("utf-8")) <= 2048 + 3  # leaves room for ellipsis bytes


@pytest.mark.asyncio
async def test_list_bundles_filters(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    unique_purpose = f"list_bundles_{uuid.uuid4().hex[:8]}"
    await repo.insert_bundle(
        BundleCreate(
            purpose=unique_purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
        )
    )
    await db_session.commit()

    svc = SnapshotBundleReadService(db_session)
    response = await svc.list_bundles(
        ListBundlesRequest(purpose=unique_purpose, limit=10)
    )
    assert response.limit == 10
    assert len(response.bundles) == 1
    assert response.bundles[0].purpose == unique_purpose


@pytest.mark.asyncio
async def test_list_snapshots_filters(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    sym = f"LS{uuid.uuid4().hex[:6]}"
    await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="symbol",
            market="kr",
            account_scope="kis_live",
            symbol=sym,
            source_kind="kis_mcp",
            payload_json={"price": 100.0, "u": str(uuid.uuid4())},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    await db_session.commit()

    svc = SnapshotBundleReadService(db_session)
    response = await svc.list_snapshots(ListSnapshotsRequest(symbol=sym, limit=5))
    assert response.limit == 5
    assert len(response.snapshots) == 1
    assert response.snapshots[0].symbol == sym
    assert response.snapshots[0].snapshot_kind == "symbol"
