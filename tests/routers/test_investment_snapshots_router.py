"""ROB-269 Phase 2 — Investment snapshots HTTP router.

GET-only router, flag-gated. The tests build a minimal FastAPI app with
just our router + the auth dependency overridden so we can exercise
endpoint behaviour without spinning the full app and SSO loop.
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
from app.routers.investment_snapshots import router as snapshots_router
from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _build_app(db_session) -> FastAPI:
    """Minimal FastAPI app for testing — uses the shared db_session fixture."""
    app = FastAPI()
    app.include_router(snapshots_router)

    async def _db_override() -> AsyncIterator:
        yield db_session

    async def _user_override() -> User:
        # Auth dependency is bypassed in tests; the value isn't read by our endpoints.
        return User(id=1, email="test@example.com", role="user")  # type: ignore[call-arg]

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = _user_override
    return app


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


async def _seed_one_bundle(db_session) -> uuid.UUID:
    repo = InvestmentSnapshotsRepository(db_session)
    purpose = f"router_test_{uuid.uuid4().hex[:8]}"
    run = await repo.insert_run(
        SnapshotRunCreate(
            purpose="report_generation",
            market="kr",
            account_scope="kis_live",
            requested_by="user",
            policy_version="intraday_action_report_v1",
        )
    )
    snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="portfolio",
            market="kr",
            account_scope="kis_live",
            source_kind="manual",
            payload_json={"cash_krw": 1, "u": str(uuid.uuid4())},
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
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    await db_session.commit()
    return bundle.bundle_uuid


@pytest.mark.asyncio
async def test_get_bundle_returns_200_with_items(db_session):
    bundle_uuid = await _seed_one_bundle(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-snapshots/bundles/{bundle_uuid}"
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["bundle"]["bundle_uuid"] == str(bundle_uuid)
    assert len(body["items"]) == 1
    assert body["payload_previews"] is None


@pytest.mark.asyncio
async def test_get_bundle_returns_404_for_unknown_uuid(db_session):
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-snapshots/bundles/{uuid.uuid4()}"
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_bundle_with_payload_preview_query_param(db_session):
    bundle_uuid = await _seed_one_bundle(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-snapshots/bundles/{bundle_uuid}",
            params={"include_payload_preview": "true"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["payload_previews"] is not None
    assert len(body["payload_previews"]) == 1


@pytest.mark.asyncio
async def test_list_bundles_returns_200(db_session):
    await _seed_one_bundle(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            "/trading/api/investment-snapshots/bundles",
            params={"limit": 5},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 5
    assert isinstance(body["bundles"], list)


@pytest.mark.asyncio
async def test_list_snapshots_returns_200(db_session):
    await _seed_one_bundle(db_session)
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            "/trading/api/investment-snapshots/snapshots",
            params={"limit": 5},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 5
    assert isinstance(body["snapshots"], list)


@pytest.mark.asyncio
async def test_list_bundles_rejects_limit_above_100(db_session):
    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            "/trading/api/investment-snapshots/bundles",
            params={"limit": 9999},
        )
    # FastAPI Query(le=100) rejects with 422.
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_router_has_no_post_put_delete_routes(db_session):
    """Read-only invariant — no write verbs exposed at HTTP layer."""
    _ = db_session  # noqa: F841
    methods = set()
    for route in snapshots_router.routes:
        if hasattr(route, "methods"):
            methods |= route.methods  # type: ignore[attr-defined]
    # HEAD/OPTIONS get added automatically by FastAPI; assert no write verbs.
    write_verbs = {"POST", "PUT", "DELETE", "PATCH"}
    overlap = methods & write_verbs
    assert overlap == set(), f"Snapshots router exposes write verbs: {overlap}"
