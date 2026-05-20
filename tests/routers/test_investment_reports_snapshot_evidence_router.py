"""ROB-275 — Router-level tests for snapshot evidence endpoints.

Same direct-handler-invocation pattern as
``tests/test_investment_reports_router.py`` — no TestClient, dependencies
supplied manually. Uses ``db_session`` because we need both
investment_reports and investment_snapshot_* tables.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers.investment_reports import (
    get_investment_report_snapshot_bundle,
    get_investment_report_snapshot_detail,
)
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

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")

_USER = SimpleNamespace(username="operator-test", id=1)
_NOW = dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)


async def _seed_report_with_bundle(db_session):
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
            payload_json={"cash_krw": 1_000, "u": str(_uuid.uuid4())},
            as_of=_NOW,
            freshness_status="fresh",
        )
    )
    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob275_router_{_uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="complete",
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
        created_by_profile="rob275-router-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )
    await db_session.commit()
    return report.report_uuid, snap.snapshot_uuid


async def _seed_report_without_bundle(db_session):
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob275-router-test",
        title="t",
        summary="s",
    )
    await db_session.commit()
    return report.report_uuid


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_404_for_unknown_report(db_session):
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_bundle(
            report_uuid=_uuid.uuid4(), _user=_USER, service=service
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_legacy_shape_for_report_without_bundle(
    db_session,
):
    report_uuid = await _seed_report_without_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_bundle(
        report_uuid=report_uuid, _user=_USER, service=service
    )
    assert response.legacy_no_snapshot is True
    assert response.bundle is None
    assert response.items == []


@pytest.mark.asyncio
async def test_snapshot_bundle_returns_full_response_for_report_with_bundle(
    db_session,
):
    report_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_bundle(
        report_uuid=report_uuid, _user=_USER, service=service
    )
    assert response.legacy_no_snapshot is False
    assert response.bundle is not None
    assert len(response.items) == 1
    assert response.items[0].snapshot_uuid == snap_uuid


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_unknown_report(db_session):
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=_uuid.uuid4(),
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_report_without_bundle(db_session):
    report_uuid = await _seed_report_without_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=report_uuid,
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_404_for_non_member_snapshot(db_session):
    report_uuid, _snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    with pytest.raises(HTTPException) as exc:
        await get_investment_report_snapshot_detail(
            report_uuid=report_uuid,
            snapshot_uuid=_uuid.uuid4(),
            _user=_USER,
            service=service,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_snapshot_detail_returns_200_with_payload_for_member(db_session):
    report_uuid, snap_uuid = await _seed_report_with_bundle(db_session)
    service = InvestmentReportQueryService(db_session)
    response = await get_investment_report_snapshot_detail(
        report_uuid=report_uuid,
        snapshot_uuid=snap_uuid,
        _user=_USER,
        service=service,
    )
    assert response.snapshot_uuid == snap_uuid
    assert response.role == "required"
    assert response.payload_json["cash_krw"] == 1_000
