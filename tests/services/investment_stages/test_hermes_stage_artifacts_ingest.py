"""Unit + integration tests for ``HermesStageArtifactsIngestService``
(ROB-287, locked decisions D1/D3/D4/D5/D8 + TS6).

These tests use the real ``db_session`` fixture so the AppendOnly
behaviour rides on the actual ``(run_uuid, stage_type)`` UNIQUE
constraint. The snapshot-bundle repository is mocked because bundle
creation is tangential to the stage-artifact write path; the service
only consults it for an existence check.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.hermes_composition import (
    HERMES_STAGE_ARTIFACTS_VERSION,
    HermesStageArtifactsIngestRequest,
    HermesStageRunEnvelope,
)
from app.schemas.investment_stages import (
    StageArtifactPayload,
    StageCitation,
    StageVerdict,
)
from app.services.investment_stages.hermes_ingest import (
    HermesStageArtifactsIngestError,
    HermesStageArtifactsIngestService,
)
from app.services.investment_stages.repository import InvestmentStagesRepository


def _bundle_row(bundle_uuid: uuid.UUID) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        bundle_uuid=bundle_uuid,
        coverage_summary={"news": {"status": "fresh"}},
        freshness_summary={"overall": "fresh"},
        status="complete",
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
    )


def _snapshots_repo_with_bundle(bundle_uuid: uuid.UUID) -> AsyncMock:
    repo = AsyncMock()
    repo.get_bundle_by_uuid = AsyncMock(return_value=_bundle_row(bundle_uuid))
    return repo


def _snapshots_repo_missing_bundle() -> AsyncMock:
    repo = AsyncMock()
    repo.get_bundle_by_uuid = AsyncMock(return_value=None)
    return repo


def _envelope(
    *,
    run_uuid: uuid.UUID,
    bundle_uuid: uuid.UUID,
    market: str = "kr",
    market_session: str | None = "regular",
    account_scope: str | None = "kis_live",
    policy_version: str = "intraday_action_report_v1",
    generator_version: str = HERMES_STAGE_ARTIFACTS_VERSION,
) -> HermesStageRunEnvelope:
    return HermesStageRunEnvelope(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
        market=market,
        market_session=market_session,
        account_scope=account_scope,
        policy_version=policy_version,
        generator_version=generator_version,
        hermes_run_id="hermes-run-1",
    )


def _payload(
    *,
    stage_type: str,
    verdict: StageVerdict = StageVerdict.NEUTRAL,
    confidence: int = 55,
    summary: str = "ok",
    cited_snapshots: list[StageCitation] | None = None,
) -> StageArtifactPayload:
    return StageArtifactPayload(
        stage_type=stage_type,
        verdict=verdict,
        confidence=confidence,
        summary=summary,
        cited_snapshots=cited_snapshots or [],
    )


# ---------------------------------------------------------------------------
# TS1: happy path — multiple artifacts on a new run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts1_ingest_creates_run_and_persists_artifacts(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid)
    )
    request = HermesStageArtifactsIngestRequest(
        run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
        artifacts=[
            _payload(stage_type="market", verdict=StageVerdict.BULL, confidence=60),
            _payload(stage_type="news", verdict=StageVerdict.NEUTRAL, confidence=40),
        ],
    )

    response = await svc.ingest_stage_artifacts(request)
    assert response.run.run_uuid == run_uuid
    assert response.run.status == "running"
    assert response.run.snapshot_bundle_uuid == bundle_uuid
    assert [r.stage_type for r in response.results] == ["market", "news"]
    assert all(not r.idempotent_existing for r in response.results)

    repo = InvestmentStagesRepository(db_session)
    persisted = await repo.list_artifacts_for_run(run_uuid)
    assert {a.stage_type for a in persisted} == {"market", "news"}


# ---------------------------------------------------------------------------
# TS2: same key + same payload → idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts2_same_key_same_payload_is_idempotent(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid)
    )
    artifact = _payload(stage_type="market", verdict=StageVerdict.BULL, confidence=60)

    first = await svc.ingest_stage_artifacts(
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[artifact],
        )
    )
    second = await svc.ingest_stage_artifacts(
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[artifact],
        )
    )

    assert first.run.run_uuid == second.run.run_uuid
    assert first.results[0].idempotent_existing is False
    assert second.results[0].idempotent_existing is True
    assert (
        second.results[0].artifact.artifact_uuid
        == first.results[0].artifact.artifact_uuid
    )


# ---------------------------------------------------------------------------
# TS3: same key + different payload → conflict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts3_same_key_different_payload_rejected(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid)
    )
    await svc.ingest_stage_artifacts(
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[
                _payload(stage_type="market", verdict=StageVerdict.BULL, confidence=60)
            ],
        )
    )

    with pytest.raises(HermesStageArtifactsIngestError) as excinfo:
        await svc.ingest_stage_artifacts(
            HermesStageArtifactsIngestRequest(
                run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
                artifacts=[
                    # Same stage_type, different verdict/confidence — must reject.
                    _payload(
                        stage_type="market",
                        verdict=StageVerdict.BEAR,
                        confidence=30,
                    )
                ],
            )
        )
    assert excinfo.value.code == "artifact_content_conflict"


# ---------------------------------------------------------------------------
# TS4: existing run + inconsistent envelope → reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts4_existing_run_inconsistent_envelope_rejected(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid)
    )
    await svc.ingest_stage_artifacts(
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(
                run_uuid=run_uuid, bundle_uuid=bundle_uuid, market="kr"
            ),
            artifacts=[_payload(stage_type="market")],
        )
    )

    with pytest.raises(HermesStageArtifactsIngestError) as excinfo:
        await svc.ingest_stage_artifacts(
            HermesStageArtifactsIngestRequest(
                # Same run_uuid, different market.
                run_envelope=_envelope(
                    run_uuid=run_uuid, bundle_uuid=bundle_uuid, market="crypto"
                ),
                artifacts=[_payload(stage_type="news")],
            )
        )
    assert excinfo.value.code == "run_envelope_mismatch"
    assert "market" in str(excinfo.value)


# ---------------------------------------------------------------------------
# TS5: missing bundle → reject
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts5_missing_snapshot_bundle_rejected(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_missing_bundle()
    )
    with pytest.raises(HermesStageArtifactsIngestError) as excinfo:
        await svc.ingest_stage_artifacts(
            HermesStageArtifactsIngestRequest(
                run_envelope=_envelope(run_uuid=uuid.uuid4(), bundle_uuid=bundle_uuid),
                artifacts=[_payload(stage_type="market")],
            )
        )
    assert excinfo.value.code == "snapshot_bundle_not_found"


# ---------------------------------------------------------------------------
# TS6: empty artifacts list → schema-level reject
# ---------------------------------------------------------------------------


def test_ts6_empty_artifacts_list_rejected_at_schema() -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    with pytest.raises(Exception) as excinfo:
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[],
        )
    # Pydantic v2 phrasing — accept any of the listed surfaces.
    msg = str(excinfo.value)
    assert "at least 1" in msg or "min_length" in msg or "too_short" in msg


# ---------------------------------------------------------------------------
# TS7: artifacts may arrive in any order (no ordering enforced)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts7_unordered_ingest_allowed(db_session) -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    svc = HermesStageArtifactsIngestService(
        db_session, snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid)
    )
    # Bull reducer arrives before market — auto_trader must not enforce
    # stage ordering at the ingest layer.
    response = await svc.ingest_stage_artifacts(
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[
                _payload(stage_type="bull_reducer", verdict=StageVerdict.BULL),
                _payload(stage_type="market", verdict=StageVerdict.NEUTRAL),
            ],
        )
    )
    assert {r.stage_type for r in response.results} == {"bull_reducer", "market"}


# ---------------------------------------------------------------------------
# TS8: composition ingest auto-finalises the matching stage run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts8_composition_ingest_finalises_running_stage_run(db_session) -> None:
    from app.schemas.hermes_composition import (
        HermesCompositionIngestRequest,
        HermesCompositionResult,
    )
    from app.schemas.investment_reports import IngestReportItem
    from app.services.investment_stages.hermes_ingest import (
        HermesCompositionIngestService,
    )

    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()

    # Seed a Hermes stage run in "running" state.
    stages_repo = InvestmentStagesRepository(db_session)
    await stages_repo.create_run(
        run_uuid=run_uuid,
        snapshot_bundle_uuid=bundle_uuid,
        market="kr",
        market_session="regular",
        account_scope="kis_live",
    )
    await db_session.flush()

    # Build a composition that references the same run via metadata.
    composition = HermesCompositionResult(
        snapshot_bundle_uuid=bundle_uuid,
        hermes_run_id="hermes-1",
        title="t",
        summary="s",
        metadata={"investment_stage_run_uuid": str(run_uuid)},
        items=[
            IngestReportItem(
                client_item_key="x",
                item_kind="risk",
                intent="risk_review",
                operation="review",
                rationale="r",
                apply_policy="requires_user_approval",
            )
        ],
    )

    # Mock the report ingestion service so we don't need the report tables.
    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=SimpleNamespace(report_uuid=uuid.uuid4()))

    svc = HermesCompositionIngestService(
        db_session,
        ingestion_service=ingestion,
        snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid),
    )
    await svc.ingest_composition(
        HermesCompositionIngestRequest(
            composition=composition,
            kst_date="2026-05-21",
            market="kr",
            market_session="regular",
            account_scope="kis_live",
        )
    )

    refreshed = await stages_repo.get_run(run_uuid)
    assert refreshed.status == "completed"
    assert refreshed.completed_at is not None


# ---------------------------------------------------------------------------
# TS9: composition without matching run — no auto-finalisation, no error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ts9_composition_without_matching_run_does_not_break(db_session) -> None:
    from app.schemas.hermes_composition import (
        HermesCompositionIngestRequest,
        HermesCompositionResult,
    )
    from app.services.investment_stages.hermes_ingest import (
        HermesCompositionIngestService,
    )

    bundle_uuid = uuid.uuid4()
    unknown_run_uuid = uuid.uuid4()

    composition = HermesCompositionResult(
        snapshot_bundle_uuid=bundle_uuid,
        hermes_run_id="hermes-1",
        title="t",
        summary="s",
        # Run UUID referenced but no run row exists — must noop.
        metadata={"investment_stage_run_uuid": str(unknown_run_uuid)},
        items=[],
    )

    ingestion = AsyncMock()
    ingestion.ingest = AsyncMock(return_value=SimpleNamespace(report_uuid=uuid.uuid4()))

    svc = HermesCompositionIngestService(
        db_session,
        ingestion_service=ingestion,
        snapshots_repository=_snapshots_repo_with_bundle(bundle_uuid),
    )
    # Must not raise.
    await svc.ingest_composition(
        HermesCompositionIngestRequest(
            composition=composition,
            kst_date="2026-05-21",
            market="kr",
        )
    )

    # No phantom run was created.
    stages_repo = InvestmentStagesRepository(db_session)
    assert await stages_repo.get_run(unknown_run_uuid) is None


# ---------------------------------------------------------------------------
# Schema-level: duplicate stage_type in a single call rejected
# ---------------------------------------------------------------------------


def test_duplicate_stage_type_in_single_call_rejected() -> None:
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    with pytest.raises(Exception) as excinfo:
        HermesStageArtifactsIngestRequest(
            run_envelope=_envelope(run_uuid=run_uuid, bundle_uuid=bundle_uuid),
            artifacts=[
                _payload(stage_type="market", verdict=StageVerdict.BULL),
                _payload(stage_type="market", verdict=StageVerdict.BEAR),
            ],
        )
    assert "duplicate" in str(excinfo.value).lower()
