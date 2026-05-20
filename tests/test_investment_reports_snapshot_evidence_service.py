"""ROB-275 — Snapshot evidence service tests.

Uses the global ``db_session`` fixture (creates every table via
``Base.metadata.create_all``) because the test exercises both
``review.investment_reports`` *and* ``review.investment_snapshot_*``
tables. The ``_investment_reports_helpers.session`` fixture only owns
the 5 investment-report tables and is not suitable here.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import (
    InvestmentReportsRepository,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)

_NOW = dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)


async def _seed_report_with_bundle(db_session):
    """Seed one report with snapshot_bundle_uuid → one required snapshot."""
    snap_repo = InvestmentSnapshotsRepository(db_session)
    run = await snap_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap = await snap_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1_000_000, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="partial",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )

    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
        snapshot_policy_version="intraday_action_report_v1",
        unavailable_sources={"naver_remote_debug": "blocked"},
        source_conflicts={"market": {"kis_mcp": 1.0, "manual": 1.1}},
    )
    await db_session.commit()
    return report.report_uuid, bundle.bundle_uuid, snap.snapshot_uuid


async def _seed_report_without_bundle(db_session):
    """Seed one report with no ``snapshot_bundle_uuid`` (legacy path)."""
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-test",
        title="t",
        summary="s",
    )
    await db_session.commit()
    return report.report_uuid


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_none_for_unknown_report(db_session):
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_bundle(_uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_legacy_for_no_bundle(db_session):
    report_uuid = await _seed_report_without_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    response = await svc.get_report_snapshot_bundle(report_uuid)
    assert response is not None
    assert response.legacy_no_snapshot is True
    assert response.bundle is None
    assert response.items == []


@pytest.mark.asyncio
async def test_get_report_snapshot_bundle_returns_bundle_and_items(db_session):
    report_uuid, bundle_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    response = await svc.get_report_snapshot_bundle(report_uuid)
    assert response is not None
    assert response.legacy_no_snapshot is False
    bundle = response.bundle
    assert bundle is not None
    assert bundle.bundle_uuid == bundle_uuid
    assert bundle.status == "partial"
    assert bundle.market == "kr"
    assert bundle.account_scope == "kis_live"
    items = response.items
    assert len(items) == 1
    item = items[0]
    assert item.snapshot_uuid == snap_uuid
    assert item.role == "required"
    assert item.snapshot_kind == "portfolio"
    assert item.payload_size_bytes is not None
    assert item.payload_size_bytes > 0
    # unavailable_sources / source_conflicts come from the *report row*,
    # not from the bundle — the viewer surfaces them separately.
    assert response.unavailable_sources == {"naver_remote_debug": "blocked"}
    assert response.source_conflicts == {"market": {"kis_mcp": 1.0, "manual": 1.1}}


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_for_unknown_report(db_session):
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_detail(_uuid.uuid4(), _uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_when_report_has_no_bundle(
    db_session,
):
    report_uuid = await _seed_report_without_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    assert await svc.get_report_snapshot_detail(report_uuid, _uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_payload_for_member(db_session):
    report_uuid, _bundle_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    svc = InvestmentReportQueryService(db_session)
    detail = await svc.get_report_snapshot_detail(report_uuid, snap_uuid)
    assert detail is not None
    assert detail.snapshot_uuid == snap_uuid
    assert detail.role == "required"
    assert detail.snapshot_kind == "portfolio"
    assert detail.payload_json["cash_krw"] == 1_000_000


@pytest.mark.asyncio
async def test_get_report_snapshot_detail_returns_none_for_non_member_snapshot(
    db_session,
):
    """A snapshot_uuid that exists but is NOT in this report's bundle → None (router → 404)."""
    report_uuid, _bundle_uuid, _snap_uuid = await _seed_report_with_bundle(db_session)

    # Create a second snapshot under a DIFFERENT bundle.
    snap_repo = InvestmentSnapshotsRepository(db_session)
    run = await snap_repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    other_snap = await snap_repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="market",
            market="kr",
            source_kind="domain_ref",
            source_table="market_quote_snapshots",
            source_id=99,
            source_uri=f"market_quote_snapshots:{_uuid.uuid4().hex[:6]}",
            payload_json={"kospi": 2700.0, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    other_bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_other_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="complete",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=other_bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=other_snap.snapshot_uuid, role="required"),
    )
    await db_session.commit()

    svc = InvestmentReportQueryService(db_session)
    detail = await svc.get_report_snapshot_detail(report_uuid, other_snap.snapshot_uuid)
    assert detail is None
