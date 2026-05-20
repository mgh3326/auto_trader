"""ROB-279 — Router-level tests for investment-stage-runs endpoints.

Uses the AsyncClient + ASGITransport pattern (same as
tests/routers/test_investment_snapshots_router.py) to exercise endpoints
over ASGI without spinning the full app and SSO loop.

Test coverage:
1. 200 + artifact list for a valid run UUID
2. 404 for unknown run UUID
3. 200 + artifacts via report→run linkage (metadata field on report)
4. 404 for unknown report UUID
5. 200 + empty artifacts for a report that has a bundle but no stage run
   (legacy fallback — bundle exists but no stage runs → empty list)
6. 404 when report has neither snapshot_bundle_uuid nor stage_run_uuid in
   metadata (new spec compliance — both paths absent → 404, not 200 empty)
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.routers.investment_stage_runs import router as stage_runs_router
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository
from app.services.investment_stages.repository import InvestmentStagesRepository

# ---------------------------------------------------------------------------
# App factory (mirrors test_investment_snapshots_router.py)
# ---------------------------------------------------------------------------


def _build_app(db_session) -> FastAPI:
    """Minimal FastAPI app for testing — uses the shared db_session fixture."""
    app = FastAPI()
    app.include_router(stage_runs_router)

    async def _db_override() -> AsyncIterator:
        yield db_session

    async def _user_override() -> User:
        return User(id=1, email="test@example.com", role="user")  # type: ignore[call-arg]

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = _user_override
    return app


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 20, 11, 0, 0, tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_run_with_artifact(db_session):
    """Create a stage run with one artifact; return (run_uuid, artifact_uuid)."""
    repo = InvestmentStagesRepository(db_session)
    run = await repo.create_run(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )
    artifact = await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(
            stage_type="market",
            verdict=StageVerdict.BULL,
            confidence=70,
            summary="bullish overall",
        ),
    )
    await db_session.flush()
    return run.run_uuid, artifact.artifact_uuid


async def _seed_report_with_stage_run(db_session):
    """Create a report whose metadata links to a stage run.

    Returns (report_uuid, stage_run_uuid, artifact_uuid).
    """
    stage_run_uuid, artifact_uuid = await _seed_run_with_artifact(db_session)

    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob279-router-test",
        title="t",
        summary="s",
        report_metadata={"investment_stage_run_uuid": str(stage_run_uuid)},
    )
    await db_session.flush()
    return report.report_uuid, stage_run_uuid, artifact_uuid


async def _seed_bundle_with_report(db_session):
    """Create a snapshot bundle and a report linked to it (no stage run).

    Returns report_uuid.
    """
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
            payload_json={"cash_krw": 1_000},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob279_bundle_{uuid.uuid4().hex[:8]}",
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )

    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob279-router-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
    )
    await db_session.flush()
    return report.report_uuid


async def _seed_report_no_bundle_no_metadata(db_session):
    """Create a report with no snapshot_bundle_uuid and no stage_run_uuid metadata.

    Returns report_uuid.
    """
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob279-router-test",
        title="t",
        summary="s",
        # snapshot_bundle_uuid intentionally omitted (defaults to None)
        # report_metadata intentionally omitted (defaults to None)
    )
    await db_session.flush()
    return report.report_uuid


async def _seed_report_with_stale_metadata_run_uuid(db_session):
    """Create a report whose metadata references a stage run UUID that does not exist.

    Returns report_uuid.
    """
    stale_run_uuid = uuid.uuid4()  # never inserted into stage runs
    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4().hex[:8]}",
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        created_by_profile="rob279-router-test",
        title="t",
        summary="s",
        report_metadata={"investment_stage_run_uuid": str(stale_run_uuid)},
    )
    await db_session.flush()
    return report.report_uuid


# ---------------------------------------------------------------------------
# Test 1: 200 + artifact list for a valid run UUID
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_stage_run_returns_200_with_artifacts(db_session):
    run_uuid, artifact_uuid = await _seed_run_with_artifact(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(f"/trading/api/investment-stage-runs/{run_uuid}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["run"]["run_uuid"] == str(run_uuid)
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["artifact_uuid"] == str(artifact_uuid)
    assert body["artifacts"][0]["stage_type"] == "market"
    assert body["artifacts"][0]["verdict"] == "bull"
    assert body["artifacts"][0]["confidence"] == 70
    # Verify new fields are present in response
    assert "run_uuid" in body["artifacts"][0]
    assert "payload_hash" in body["artifacts"][0]
    assert "raw_payload_json" in body["artifacts"][0]
    assert "created_at" in body["run"]


# ---------------------------------------------------------------------------
# Test 2: 404 for unknown run UUID
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_stage_run_returns_404_for_unknown_uuid(db_session):
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(f"/trading/api/investment-stage-runs/{uuid.uuid4()}")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "stage_run_not_found"


# ---------------------------------------------------------------------------
# Test 3: 200 + artifacts via report→run linkage through metadata field
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_report_stage_artifacts_via_metadata_linkage(db_session):
    report_uuid, stage_run_uuid, artifact_uuid = await _seed_report_with_stage_run(
        db_session
    )
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-reports/{report_uuid}/stage-artifacts"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["report_uuid"] == str(report_uuid)
    assert body["stage_run_uuid"] == str(stage_run_uuid)
    assert len(body["artifacts"]) == 1
    assert body["artifacts"][0]["artifact_uuid"] == str(artifact_uuid)
    # run_uuid in artifact should match the stage run
    assert body["artifacts"][0]["run_uuid"] == str(stage_run_uuid)


# ---------------------------------------------------------------------------
# Test 4: 404 for unknown report UUID
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_report_stage_artifacts_returns_404_for_unknown_report(db_session):
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-reports/{uuid.uuid4()}/stage-artifacts"
        )

    assert resp.status_code == 404
    assert resp.json()["detail"] == "report_not_found"


# ---------------------------------------------------------------------------
# Test 5: 200 + empty artifacts for report with bundle but no stage run
# (legacy fallback — bundle exists but no stage runs → empty list)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_report_stage_artifacts_returns_empty_for_no_stage_run(db_session):
    report_uuid = await _seed_bundle_with_report(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-reports/{report_uuid}/stage-artifacts"
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["report_uuid"] == str(report_uuid)
    assert body["stage_run_uuid"] is None
    assert body["artifacts"] == []


# ---------------------------------------------------------------------------
# Test 6: 404 when report has neither snapshot_bundle_uuid nor stage_run_uuid
# in metadata (spec compliance — both resolution paths absent → 404)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_report_stage_artifacts_404_when_no_bundle_and_no_metadata_run(
    db_session,
):
    report_uuid = await _seed_report_no_bundle_no_metadata(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-reports/{report_uuid}/stage-artifacts"
        )

    assert resp.status_code == 404
    assert "snapshot_bundle_uuid" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Test 7: 404 when report_metadata carries a stage_run_uuid that no longer
# exists in the DB (stale link — deleted/migrated run)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_get_report_stage_artifacts_404_when_metadata_run_uuid_stale(
    db_session,
):
    report_uuid = await _seed_report_with_stale_metadata_run_uuid(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-reports/{report_uuid}/stage-artifacts"
        )

    assert resp.status_code == 404
    assert "non-existent" in resp.json()["detail"]
