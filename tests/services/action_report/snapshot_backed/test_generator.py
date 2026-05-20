"""ROB-273 — SnapshotBackedReportGenerator service tests.

The generator is tested with hand-rolled fakes for the bundle-ensure and
ingestion services so each test stays focused on the orchestration
contract. End-to-end coverage with a real DB lives in the existing
``test_bundle_ensure_service`` + ``test_investment_reports_*`` suites.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

import pytest

from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.schemas.investment_snapshots_mcp import (
    EnsureBundleRequest,
    EnsureBundleResponse,
)
from app.services.action_report.snapshot_backed.generator import (
    PublishBlockedByStaleGateError,
    SnapshotBackedReportGenerator,
    SnapshotBackedReportGeneratorError,
)
from app.services.action_report.snapshot_backed.request import (
    ReportGenerationRequest,
)


class _FakeEnsureService:
    def __init__(self, response: EnsureBundleResponse) -> None:
        self.response = response
        self.calls: list[EnsureBundleRequest] = []

    async def ensure(self, request: EnsureBundleRequest) -> EnsureBundleResponse:
        self.calls.append(request)
        return self.response


class _FakeReport:
    def __init__(self, report_uuid: uuid.UUID) -> None:
        self.report_uuid = report_uuid


class _FakeIngestionService:
    def __init__(self, *, report_uuid: uuid.UUID | None = None) -> None:
        self.report_uuid = report_uuid or uuid.uuid4()
        self.calls: list[IngestReportRequest] = []

    async def ingest(self, request: IngestReportRequest):
        self.calls.append(request)
        return _FakeReport(self.report_uuid)


class _FakeSnapshotsRepository:
    """ROB-274 — stubbed repository the generator calls to read back the
    persisted bundle for classifier context.

    Default behaviour: ``get_bundle_by_uuid`` returns None, which makes the
    generator emit an empty ``ClassifierContext`` (no active watches; empty
    pending_orders unless missing_sources says otherwise). Tests that want
    to exercise specific classifier paths can pass ``bundle`` and ``items``.
    """

    def __init__(
        self,
        *,
        bundle: Any = None,
        items: list[tuple[Any, Any]] | None = None,
    ) -> None:
        self._bundle = bundle
        self._items = items or []
        self.get_bundle_calls: list[uuid.UUID] = []
        self.list_items_calls: list[int] = []

    async def get_bundle_by_uuid(self, bundle_uuid: uuid.UUID):
        self.get_bundle_calls.append(bundle_uuid)
        return self._bundle

    async def list_bundle_items_with_snapshots(self, bundle_id: int):
        self.list_items_calls.append(bundle_id)
        return list(self._items)


def _ensure_response(
    *,
    bundle_uuid: uuid.UUID | None = None,
    status: str = "complete",
    freshness_summary: dict[str, Any] | None = None,
    coverage_summary: dict[str, Any] | None = None,
    missing_sources: list[str] | None = None,
    created: bool = True,
    warnings: list[str] | None = None,
) -> EnsureBundleResponse:
    return EnsureBundleResponse(
        bundle_uuid=bundle_uuid or uuid.uuid4(),
        status=status,  # type: ignore[arg-type]
        created=created,
        coverage_summary=coverage_summary
        or {
            "required": {
                "portfolio": "fresh",
                "journal": "fresh",
                "watch_context": "fresh",
                "market": "fresh",
            },
            "optional": {},
        },
        freshness_summary=freshness_summary
        or {
            "overall": "fresh",
            "portfolio": {"status": "fresh"},
            "journal": {"status": "fresh"},
            "watch_context": {"status": "fresh"},
            "market": {"status": "fresh"},
        },
        missing_sources=missing_sources or [],
        warnings=warnings or [],
        run_uuid=None,
    )


def _make_request(**overrides: Any) -> ReportGenerationRequest:
    base = {
        "market": "kr",
        "account_scope": "kis_live",
        "status": "published",
        "created_by_profile": "test-runner",
        "title": "Snapshot-backed KR advisory",
        "summary": "테스트 요약",
        "kst_date": "2026-05-19",
        "items": [],
    }
    base.update(overrides)
    return ReportGenerationRequest.model_validate(base)


@pytest.mark.asyncio
async def test_happy_path_kr_published(monkeypatch: pytest.MonkeyPatch) -> None:
    """Required kinds all fresh → published report persists with snapshot metadata."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()

    gen = SnapshotBackedReportGenerator(
        session=object(),  # not used by fakes
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request())

    assert response.report_uuid == ingest.report_uuid
    assert response.bundle_status == "complete"
    assert response.snapshot_freshness_summary["overall"] == "fresh"
    assert response.items_count == 0
    assert response.warnings == []
    assert response.bundle_reused is False
    assert response.unavailable_sources == {}

    # Ingestion service received the snapshot metadata round-trip.
    assert len(ingest.calls) == 1
    sent = ingest.calls[0]
    assert sent.snapshot_bundle_uuid == ensure.response.bundle_uuid
    assert sent.snapshot_policy_version == "intraday_action_report_v1"
    assert sent.snapshot_coverage_summary == response.snapshot_coverage_summary
    assert sent.snapshot_freshness_summary["overall"] == "fresh"
    assert sent.metadata.get("snapshot_backed_generator") is True


@pytest.mark.asyncio
async def test_happy_path_crypto_published() -> None:
    """Crypto/upbit_live pairing is also accepted."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(
        _make_request(market="crypto", account_scope="upbit_live")
    )
    assert response.report_uuid == ingest.report_uuid
    assert ensure.calls[0].market == "crypto"
    assert ensure.calls[0].account_scope == "upbit_live"


@pytest.mark.asyncio
async def test_unsupported_market_account_pair_rejected() -> None:
    """US/kis_live or crypto/kis_live etc. are rejected at request validation."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    req = _make_request(market="crypto", account_scope="kis_live")
    with pytest.raises(SnapshotBackedReportGeneratorError):
        await gen.generate(req)
    assert ingest.calls == []


@pytest.mark.asyncio
async def test_published_blocked_when_bundle_failed() -> None:
    """bundle.status='failed' on a published request raises and never ingests."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="failed",
            freshness_summary={
                "overall": "failed",
                "portfolio": {"status": "unavailable"},
            },
            missing_sources=["portfolio"],
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    with pytest.raises(PublishBlockedByStaleGateError) as exc_info:
        await gen.generate(_make_request())
    assert exc_info.value.bundle_status == "failed"
    assert ingest.calls == []


@pytest.mark.asyncio
async def test_published_blocked_when_required_kind_hard_stale() -> None:
    """Critical kind hard_stale blocks even if bundle.status is 'partial'."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "partial",
                "portfolio": {"status": "fresh"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "hard_stale"},
            },
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    with pytest.raises(PublishBlockedByStaleGateError):
        await gen.generate(_make_request())
    assert ingest.calls == []


@pytest.mark.asyncio
async def test_draft_status_permitted_even_on_hard_stale() -> None:
    """Draft reports are NOT subject to the published-only block."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "hard_stale",
                "portfolio": {"status": "hard_stale"},
            },
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request(status="draft"))
    assert response.snapshot_freshness_summary["overall"] == "hard_stale"
    assert len(ingest.calls) == 1


@pytest.mark.asyncio
async def test_optional_collector_failure_degrades_but_does_not_block() -> None:
    """Bundle.status='partial' from optional-kind failure still publishes."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "partial",
                "portfolio": {"status": "fresh"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
                "invest_page": {"status": "unavailable"},
                "news": {"status": "soft_stale"},
            },
            missing_sources=["invest_page"],
            warnings=["invest_page: collector timed out"],
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request())
    assert response.bundle_status == "partial"
    assert response.snapshot_freshness_summary["overall"] == "partial"
    assert "invest_page" in response.unavailable_sources
    assert response.warnings == ["invest_page: collector timed out"]
    assert len(ingest.calls) == 1


@pytest.mark.asyncio
async def test_jsonb_normalisation_runs_on_items() -> None:
    """Decimal / datetime / UUID inside item evidence_snapshot becomes JSONB-safe."""
    from decimal import Decimal

    item = IngestReportItem(
        client_item_key="i1",
        item_kind="risk",
        intent="risk_review",
        rationale="risk note",
        evidence_snapshot={
            "p": Decimal("100.5"),
            "at": dt.datetime(2026, 5, 19, tzinfo=dt.UTC),
        },
        metadata={"id": uuid.UUID("aaaaaaaa-1234-5678-9abc-def012345678")},
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    await gen.generate(_make_request(items=[item]))

    sent = ingest.calls[0]
    assert sent.items[0].evidence_snapshot["p"] == "100.5"
    assert sent.items[0].evidence_snapshot["at"] == "2026-05-19T00:00:00+00:00"
    assert sent.items[0].metadata["id"] == "aaaaaaaa-1234-5678-9abc-def012345678"


@pytest.mark.asyncio
async def test_ensure_response_with_no_bundle_raises() -> None:
    ensure = _FakeEnsureService(
        EnsureBundleResponse(
            bundle_uuid=None,
            status="failed",  # type: ignore[arg-type]
            created=False,
            warnings=["upstream broken"],
            run_uuid=None,
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    with pytest.raises(SnapshotBackedReportGeneratorError):
        await gen.generate(_make_request())
    assert ingest.calls == []


@pytest.mark.asyncio
async def test_generator_classifies_items_against_active_watches_and_pending_orders():
    """ROB-274 — classifier runs between bundle-ensure and ingest.

    Wires a fake bundle whose watch_context snapshot contains an active
    alert that matches the draft watch item. The classifier should emit
    operation='keep' with a target_ref pointing at the matched alert.

    The bundle does NOT contain a pending_orders snapshot — the generator
    must surface that as ``pending_orders=None`` (unavailable) to the
    classifier so action items would be downgraded to ``review``. (Here
    we only check the watch path; the pending_orders=None path is
    indirectly verified by the absence of action items.)
    """

    from decimal import Decimal
    from types import SimpleNamespace

    from app.schemas.investment_reports import WatchConditionPayload

    matched_alert_uuid = uuid.uuid4()

    draft_items = [
        IngestReportItem(
            client_item_key="w-1",
            item_kind="watch",
            symbol="KRW-BTC",
            intent="trend_recovery_review",
            rationale="watch trend recovery",
            watch_condition=WatchConditionPayload(
                metric="price",
                operator="above",
                threshold=Decimal("100"),
            ),
            valid_until=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7),
        ),
    ]

    ensure = _FakeEnsureService(_ensure_response())

    # Fake the bundle row + a watch_context snapshot whose payload contains
    # an active alert matching the draft item's symbol+metric+threshold.
    fake_bundle = SimpleNamespace(id=4242)
    fake_watch_item = SimpleNamespace(id=1, snapshot_id=10)
    fake_watch_snapshot = SimpleNamespace(
        snapshot_kind="watch_context",
        payload_json={
            "active_alerts": [
                {
                    "alert_uuid": str(matched_alert_uuid),
                    "symbol": "KRW-BTC",
                    "metric": "price",
                    "operator": "above",
                    "threshold": "100",
                    "threshold_key": "100",
                    "action_mode": "notify_only",
                    "intent": "trend_recovery_review",
                    "status": "active",
                    "valid_until": (
                        dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7)
                    ).isoformat(),
                    "activated_at": dt.datetime.now(tz=dt.UTC).isoformat(),
                }
            ]
        },
    )
    fake_repo = _FakeSnapshotsRepository(
        bundle=fake_bundle,
        items=[(fake_watch_item, fake_watch_snapshot)],
    )

    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )

    # Use draft to skip the published guard.
    response = await gen.generate(
        _make_request(
            market="crypto",
            account_scope="upbit_live",
            status="draft",
            items=draft_items,
        )
    )

    assert response.items_count == 1

    # Verify the generator actually queried the bundle back from the repo.
    assert fake_repo.get_bundle_calls == [ensure.response.bundle_uuid]
    assert fake_repo.list_items_calls == [fake_bundle.id]

    # The classifier should have rewritten the watch item to operation='keep'
    # because the bundle's active_alerts has a matching alert at the same
    # threshold/metric/operator.
    assert len(ingest.calls) == 1
    sent = ingest.calls[0]
    sent_item = sent.items[0]
    assert sent_item.operation == "keep"
    # target_ref must be populated with the matched alert id.
    assert sent_item.target_ref is not None
    assert sent_item.target_ref.type == "investment_watch_alert"
    assert sent_item.target_ref.id == str(matched_alert_uuid)
    # current_state was captured from the bundle's active_alerts entry.
    assert sent_item.current_state is not None
    assert sent_item.current_state["metric"] == "price"
    assert sent_item.current_state["threshold"] == "100"
    # apply_policy default for proposals referencing existing state.
    assert sent_item.apply_policy == "requires_user_approval"


@pytest.mark.asyncio
async def test_generator_surfaces_unavailable_pending_orders_to_classifier():
    """ROB-274 — when pending_orders is in missing_sources, action items
    are downgraded to operation='review' with '확인 불가' rationale."""

    action_item = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        symbol="KRW-BTC",
        side="buy",
        intent="buy_review",
        rationale="buy on dip",
    )

    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "partial",
                "portfolio": {"status": "fresh"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
                "pending_orders": {"status": "unavailable"},
            },
            missing_sources=["pending_orders"],
        )
    )
    # Repository returns no bundle items (no pending_orders snapshot
    # persisted); combined with missing_sources, classifier sees None.
    fake_repo = _FakeSnapshotsRepository(bundle=None)

    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )

    await gen.generate(
        _make_request(
            market="crypto",
            account_scope="upbit_live",
            status="draft",
            items=[action_item],
        )
    )

    assert len(ingest.calls) == 1
    sent_item = ingest.calls[0].items[0]
    assert sent_item.operation == "review"
    assert "확인 불가" in sent_item.rationale
