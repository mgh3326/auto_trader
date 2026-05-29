"""Unit tests for the Hermes context exporter (ROB-287)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.hermes_composition import (
    HERMES_CONTEXT_VERSION,
    HermesContextPayload,
)
from app.schemas.investment_stages import StageArtifactPayload, StageVerdict
from app.services.investment_stages.hermes_context import (
    HermesContextExporter,
    HermesContextExportError,
)
from app.services.investment_stages.stages.base import (
    StageContext,
    UnavailableStageError,
)


class _ConstantStage:
    def __init__(
        self,
        *,
        stage_type: str,
        verdict: StageVerdict = StageVerdict.NEUTRAL,
        confidence: int = 50,
    ) -> None:
        self.stage_type = stage_type
        self._verdict = verdict
        self._confidence = confidence

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        return StageArtifactPayload(
            stage_type=self.stage_type,
            verdict=self._verdict,
            confidence=self._confidence,
            summary=f"{self.stage_type} ok",
        )


class _AlwaysUnavailableStage:
    stage_type = "watch_context"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        raise UnavailableStageError("watch_context snapshot missing")


class _CapturingMarketStage:
    stage_type = "market"

    def __init__(self) -> None:
        self.seen_market: object = "UNSET"

    async def run(self, ctx: StageContext) -> StageArtifactPayload:
        self.seen_market = ctx.market
        return StageArtifactPayload(
            stage_type="market", verdict=StageVerdict.NEUTRAL, confidence=20
        )


def _make_bundle(*, status: str = "complete") -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        bundle_uuid=uuid.uuid4(),
        market="crypto",
        account_scope="upbit_live",
        policy_version="intraday_action_report_v1",
        coverage_summary={"news": {"status": "fresh"}},
        freshness_summary={"overall": "fresh"},
        status=status,
    )


def _make_snapshot(kind: str) -> SimpleNamespace:
    return SimpleNamespace(
        snapshot_uuid=uuid.uuid4(),
        snapshot_kind=kind,
        payload_json={},
    )


@pytest.mark.asyncio
async def test_exporter_plumbs_bundle_market_into_stage_context() -> None:
    # ROB-366 B5: MarketStage must see the bundle market to select US vs KR.
    bundle = _make_bundle()  # market="crypto"
    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = bundle
    repo.list_bundle_items_with_snapshots.return_value = [
        (object(), _make_snapshot("market")),
    ]
    cap = _CapturingMarketStage()
    exporter = HermesContextExporter(
        session=AsyncMock(), snapshots_repository=repo, stages=[cap]
    )
    await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)
    assert cap.seen_market == "crypto"


@pytest.mark.asyncio
async def test_exporter_builds_frozen_payload_with_stage_inputs() -> None:
    bundle = _make_bundle()
    snap_news = _make_snapshot("news")
    snap_portfolio = _make_snapshot("portfolio")

    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = bundle
    repo.list_bundle_items_with_snapshots.return_value = [
        (object(), snap_news),
        (object(), snap_portfolio),
    ]

    exporter = HermesContextExporter(
        session=AsyncMock(),
        snapshots_repository=repo,
        stages=[
            _ConstantStage(
                stage_type="market", verdict=StageVerdict.BULL, confidence=60
            ),
            _ConstantStage(stage_type="news", verdict=StageVerdict.NEUTRAL),
        ],
    )

    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert isinstance(payload, HermesContextPayload)
    assert payload.context_version == HERMES_CONTEXT_VERSION
    assert payload.snapshot_bundle_uuid == bundle.bundle_uuid
    assert payload.bundle_status == "complete"
    assert payload.market == "crypto"
    assert payload.account_scope == "upbit_live"
    assert payload.freshness_summary == {"overall": "fresh"}
    assert {entry.stage_type for entry in payload.stage_inputs} == {"market", "news"}
    market_entry = next(e for e in payload.stage_inputs if e.stage_type == "market")
    assert market_entry.artifact.verdict == StageVerdict.BULL
    assert market_entry.artifact.confidence == 60
    # Advisory-only constraints are pinned at the type level.
    assert payload.constraints.advisory_only is True
    assert payload.constraints.requires_user_approval is True
    assert payload.constraints.forbids_broker_mutation is True


@pytest.mark.asyncio
async def test_exporter_includes_report_diagnostics() -> None:
    """ROB-318 PR-B — exporter surfaces deterministic data-sufficiency signals:
    data_sufficiency_by_source + report_quality_summary + why_no_action (data
    gating only at export time)."""
    bundle = _make_bundle(status="partial")
    bundle.freshness_summary = {
        "overall": "unavailable",
        "portfolio": {"status": "unavailable", "reason_code": "user_id_missing"},
        "market": {"status": "fresh"},
    }
    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = bundle
    repo.list_bundle_items_with_snapshots.return_value = []

    exporter = HermesContextExporter(
        session=AsyncMock(), snapshots_repository=repo, stages=[]
    )
    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)

    assert (
        payload.data_sufficiency_by_source["portfolio"]["reason_code"]
        == "user_id_missing"
    )
    assert payload.report_quality_summary["grade"] == "informational_only"
    assert payload.why_no_action is not None
    assert payload.why_no_action["kind"] == "data_insufficient"
    assert payload.why_no_action["blocking_sources"] == ["portfolio"]


@pytest.mark.asyncio
async def test_exporter_raises_when_bundle_missing() -> None:
    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = None
    exporter = HermesContextExporter(
        session=AsyncMock(), snapshots_repository=repo, stages=[]
    )

    with pytest.raises(HermesContextExportError):
        await exporter.export(snapshot_bundle_uuid=uuid.uuid4())


@pytest.mark.asyncio
async def test_unavailable_stage_marked_in_payload() -> None:
    """Stages that can't run produce ``UNAVAILABLE`` artifacts the
    payload routes into ``unavailable_sources`` so Hermes sees the gap."""
    bundle = _make_bundle(status="partial")
    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = bundle
    repo.list_bundle_items_with_snapshots.return_value = []

    exporter = HermesContextExporter(
        session=AsyncMock(),
        snapshots_repository=repo,
        stages=[_AlwaysUnavailableStage()],
    )

    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)
    assert payload.bundle_status == "partial"
    watch_entry = next(
        e for e in payload.stage_inputs if e.stage_type == "watch_context"
    )
    assert watch_entry.artifact.verdict == StageVerdict.UNAVAILABLE
    assert "watch_context" in payload.unavailable_sources
    assert payload.unavailable_sources["watch_context"]["status"] == "unavailable"


@pytest.mark.asyncio
async def test_payload_carries_no_provider_or_secret_fields() -> None:
    """Sanity: the exported payload has no field that looks like a
    provider handle, API key, or model name — Hermes does its own LLM
    setup."""
    bundle = _make_bundle()
    repo = AsyncMock()
    repo.get_bundle_by_uuid.return_value = bundle
    repo.list_bundle_items_with_snapshots.return_value = []

    exporter = HermesContextExporter(
        session=AsyncMock(),
        snapshots_repository=repo,
        stages=[_ConstantStage(stage_type="market")],
    )

    payload = await exporter.export(snapshot_bundle_uuid=bundle.bundle_uuid)
    dumped = payload.model_dump()
    forbidden_substrings = ("api_key", "gemini", "openai", "model_name", "provider")
    flat_keys = " ".join(dumped.keys()).lower()
    for needle in forbidden_substrings:
        assert needle not in flat_keys, (
            f"hermes context payload leaks provider/secret-shaped key: {needle}"
        )
