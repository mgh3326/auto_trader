"""ROB-279 — Blocked-report diagnostic integration tests.

Proves that even when a stale gate would block final report ingest, the
stage run + artifacts remain queryable via the run-scoped API
(GET /trading/api/investment-stage-runs/{run_uuid}).

Two invariants tested:

1. No-report diagnostic: a stage run + artifact seeded directly via
   InvestmentStagesRepository is fully queryable even though no
   investment_reports row exists for the run.

2. Blocked-run diagnostic: a stage run whose status was explicitly set
   to "blocked" (proxy for aborted ingest) is still returned by the
   endpoint with status="blocked" and any pre-abort artifacts intact.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.routers.investment_stage_runs import router as stage_runs_router
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.repository import InvestmentStagesRepository

# ---------------------------------------------------------------------------
# App factory (mirrors tests/routers/test_investment_stage_runs.py)
# ---------------------------------------------------------------------------


def _build_app(db_session) -> FastAPI:
    app = FastAPI()
    app.include_router(stage_runs_router)

    async def _db_override() -> AsyncIterator:
        yield db_session

    async def _user_override() -> User:
        return User(id=1, email="test@example.com", role="user")  # type: ignore[call-arg]

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_authenticated_user] = _user_override
    return app


# ---------------------------------------------------------------------------
# Test 1: stage run + artifact queryable even with no investment_reports row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_run_queryable_without_report_row(db_session):
    """Forensics invariant: stage run + artifacts remain accessible even
    when no investment_reports row exists for this run.

    This is the "blocked-report still queryable" scenario — the ingest
    was never called (stale gate blocked it upstream), but the diagnostic
    artifacts persisted during the run are still retrievable.
    """
    repo = InvestmentStagesRepository(db_session)

    # Seed a stage run with one artifact directly — no report row created.
    run = await repo.create_run(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="crypto",
        market_session=None,
        account_scope="upbit_live",
    )
    artifact = await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(
            stage_type="bull_reducer",
            verdict=StageVerdict.BULL,
            confidence=65,
            summary="Bullish synthesis — no report row exists",
        ),
    )
    await repo.complete_run(run.run_uuid, status="completed")
    await db_session.flush()

    # No investment_reports row is created — this is intentional.

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-stage-runs/{run.run_uuid}"
        )

    assert resp.status_code == 200
    body = resp.json()

    # Run metadata is present.
    assert body["run"]["run_uuid"] == str(run.run_uuid)
    assert body["run"]["status"] == "completed"

    # Artifact is present and correct.
    assert len(body["artifacts"]) == 1
    art = body["artifacts"][0]
    assert art["artifact_uuid"] == str(artifact.artifact_uuid)
    assert art["stage_type"] == "bull_reducer"
    assert art["verdict"] == "bull"
    assert art["confidence"] == 65
    assert art["run_uuid"] == str(run.run_uuid)


# ---------------------------------------------------------------------------
# Test 2: blocked-status run still returns artifacts persisted before abort
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blocked_run_artifacts_still_queryable(db_session):
    """A stage run whose status is 'blocked' (stale gate aborted the final
    ingest) is still returned by the endpoint with all artifacts that were
    persisted before the abort.

    This confirms the diagnostic consumer can read partial-run state even
    when the pipeline was cut short.
    """
    repo = InvestmentStagesRepository(db_session)

    # Seed a run with two artifacts, then mark it as blocked.
    run = await repo.create_run(
        snapshot_bundle_uuid=uuid.uuid4(),
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )
    artifact_bull = await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(
            stage_type="bull_reducer",
            verdict=StageVerdict.BULL,
            confidence=72,
            summary="Bull evidence collected before abort",
        ),
    )
    artifact_bear = await repo.persist_artifact(
        run.run_uuid,
        StageArtifactPayload(
            stage_type="bear_reducer",
            verdict=StageVerdict.BEAR,
            confidence=28,
            summary="Bear evidence collected before abort",
        ),
    )

    # Simulate stale gate abort: mark run as blocked.
    await repo.complete_run(run.run_uuid, status="blocked")
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/trading/api/investment-stage-runs/{run.run_uuid}"
        )

    assert resp.status_code == 200
    body = resp.json()

    # Run is returned with blocked status.
    assert body["run"]["run_uuid"] == str(run.run_uuid)
    assert body["run"]["status"] == "blocked"
    assert body["run"]["completed_at"] is not None

    # Both pre-abort artifacts are present.
    assert len(body["artifacts"]) == 2
    artifact_uuids = {a["artifact_uuid"] for a in body["artifacts"]}
    assert str(artifact_bull.artifact_uuid) in artifact_uuids
    assert str(artifact_bear.artifact_uuid) in artifact_uuids

    stage_types = {a["stage_type"] for a in body["artifacts"]}
    assert stage_types == {"bull_reducer", "bear_reducer"}
