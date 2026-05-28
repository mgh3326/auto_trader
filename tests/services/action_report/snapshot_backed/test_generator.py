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
    assert response.items_count == 1
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
async def test_why_no_action_data_insufficient_on_unavailable_portfolio() -> None:
    """ROB-318 PR-A — unavailable required kind → why_no_action=data_insufficient."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "unavailable",
                "portfolio": {
                    "status": "unavailable",
                    "reason_code": "user_id_missing",
                },
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
            },
            missing_sources=["portfolio"],
        )
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    # draft so the publish gate does not short-circuit before the response.
    response = await gen.generate(_make_request(status="draft"))
    assert response.why_no_action is not None
    assert response.why_no_action["kind"] == "data_insufficient"
    assert response.why_no_action["blocking_sources"] == ["portfolio"]


@pytest.mark.asyncio
async def test_why_no_action_real_no_action_when_fresh_and_no_items() -> None:
    """ROB-318 PR-A — all fresh, no action items → why_no_action=real_no_action."""
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=_FakeEnsureService(_ensure_response()),
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request(status="draft"))
    assert response.why_no_action is not None
    assert response.why_no_action["kind"] == "real_no_action"


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
    """crypto/kis_live etc. are rejected at the generator's pair validator.

    Post ROB-297, ``us/kis_live`` is the canonical KIS overseas pair and
    must NOT raise; see ``test_happy_path_us_kis_live_published``.
    """
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


# ---------------------------------------------------------------------------
# ROB-297 — market="us" + account_scope="kis_live" canonical pair.
#
# Guardrails (see ROB-297 pre-implementation comment):
# - market="us" is a single market; brokers are separated by account_scope.
# - canonical KIS overseas combo is ("us", "kis_live"); no kis_overseas_live
#   alias is introduced.
# - the existing ("kr", "kis_live") and ("crypto", "upbit_live") paths must
#   keep working without regression.
# ---------------------------------------------------------------------------
def test_request_schema_accepts_us_kis_live() -> None:
    """ReportGenerationRequest accepts market='us' with account_scope='kis_live'."""
    req = _make_request(market="us", account_scope="kis_live")
    assert req.market == "us"
    assert req.account_scope == "kis_live"


@pytest.mark.asyncio
async def test_happy_path_us_kis_live_published() -> None:
    """US/kis_live published flow: validator passes, bundle ensure called with us."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request(market="us", account_scope="kis_live"))

    assert response.report_uuid == ingest.report_uuid
    assert ensure.calls[0].market == "us"
    assert ensure.calls[0].account_scope == "kis_live"
    # Round-trip: ingestion service received market="us" + account_scope="kis_live".
    assert ingest.calls[0].market == "us"
    assert ingest.calls[0].account_scope == "kis_live"


@pytest.mark.asyncio
async def test_us_upbit_live_pair_rejected() -> None:
    """us/upbit_live passes Pydantic literal check but is rejected by the validator."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    req = _make_request(market="us", account_scope="upbit_live")
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


# ---------------------------------------------------------------------------
# ROB-278 — user_id propagation and reused-freshness reproducer.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_user_id_is_forwarded_from_report_request_to_bundle_ensure() -> None:
    """ROB-278 — ReportGenerationRequest.user_id flows into EnsureBundleRequest.user_id.

    Policy: callers must pass user_id explicitly to enable kis_live broker
    reads; the generator does not invent a default.
    """
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    await gen.generate(_make_request(user_id=42))
    assert len(ensure.calls) == 1
    assert ensure.calls[0].user_id == 42


@pytest.mark.asyncio
async def test_user_id_default_is_none_and_propagates_as_none() -> None:
    """ROB-278 — omitting user_id propagates None (fail-closed downstream)."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    await gen.generate(_make_request())
    assert ensure.calls[0].user_id is None


class _FakeSymbolDerivationService:
    """Records the derive() call and returns a fixed derivation."""

    def __init__(
        self,
        *,
        symbols: list[str] | None = None,
        provenance: dict[str, Any] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._symbols = symbols if symbols is not None else []
        self._provenance = provenance or {
            "sources": {
                "seed": [],
                "portfolio": [],
                "journal": [],
                "watch": [],
                "candidate": [],
            },
            "dropped_by_cap": [],
            "cap": 50,
            "total_unique": 0,
        }

    async def derive(
        self,
        *,
        market: str,
        account_scope: str | None,
        user_id: int | None,
        seed_symbols: list[str] | None,
    ):
        self.calls.append(
            {
                "market": market,
                "account_scope": account_scope,
                "user_id": user_id,
                "seed_symbols": list(seed_symbols or []),
            }
        )
        from app.services.action_report.snapshot_backed.symbol_derivation import (
            SymbolDerivation,
        )

        return SymbolDerivation(
            symbols=list(self._symbols), provenance=self._provenance
        )


@pytest.mark.asyncio
async def test_generator_calls_symbol_derivation_and_forwards_symbols() -> None:
    """ROB-278 — derived symbols flow into EnsureBundleRequest.symbols.

    The generator unions request.symbols with derived symbols (the derivation
    service preserves seed). Provenance is stashed on the ingest metadata.
    """
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    derivation = _FakeSymbolDerivationService(
        symbols=["005930", "000660"],
        provenance={
            "sources": {
                "seed": [],
                "portfolio": ["005930"],
                "journal": ["000660"],
                "watch": [],
                "candidate": [],
            },
            "dropped_by_cap": [],
            "cap": 50,
            "total_unique": 2,
        },
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
        symbol_derivation_service=derivation,
    )
    await gen.generate(_make_request(user_id=42))

    # Derivation was called with the report's context.
    assert len(derivation.calls) == 1
    call = derivation.calls[0]
    assert call["market"] == "kr"
    assert call["account_scope"] == "kis_live"
    assert call["user_id"] == 42

    # Derived symbols reached EnsureBundleRequest.symbols.
    assert set(ensure.calls[0].symbols or []) == {"005930", "000660"}

    # Provenance is recorded on the ingest metadata for audit.
    sent = ingest.calls[0]
    assert "symbol_derivation" in sent.metadata
    assert sent.metadata["symbol_derivation"]["sources"]["portfolio"] == ["005930"]


@pytest.mark.asyncio
async def test_generator_preserves_seed_symbols_through_derivation() -> None:
    """ROB-278 — request.symbols is passed as seed_symbols and must survive."""
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    # Derivation returns seed + extra.
    derivation = _FakeSymbolDerivationService(symbols=["X", "Y"])
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
        symbol_derivation_service=derivation,
    )
    await gen.generate(_make_request(symbols=["X"], user_id=42))
    assert derivation.calls[0]["seed_symbols"] == ["X"]


@pytest.mark.asyncio
async def test_reused_bundle_freshness_does_not_downgrade_to_unavailable() -> None:
    """ROB-278 Phase 2 — bundle.status='reused' with a stored summary that
    only has per-kind statuses (no explicit ``overall``) must derive
    ``overall`` from those per-kind statuses, not from a
    ``_BUNDLE_STATUS_TO_OVERALL.get(status, 'unavailable')`` fallback.
    """
    reused_summary = {
        # Note: no 'overall' key — derivation must compute it.
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "partial"},
    }
    ensure = _FakeEnsureService(
        _ensure_response(
            status="reused",
            freshness_summary=reused_summary,
            created=False,
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
    # Worst per-kind status is 'partial' (market), so overall must be 'partial'.
    assert response.snapshot_freshness_summary["overall"] == "partial"


@pytest.mark.asyncio
async def test_reused_bundle_freshness_all_fresh_derives_fresh() -> None:
    """ROB-278 Phase 2 — all per-kind 'fresh' derives overall='fresh'."""
    reused_summary = {
        "portfolio": {"status": "fresh"},
        "journal": {"status": "fresh"},
        "watch_context": {"status": "fresh"},
        "market": {"status": "fresh"},
    }
    ensure = _FakeEnsureService(
        _ensure_response(
            status="reused", freshness_summary=reused_summary, created=False
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
    assert response.snapshot_freshness_summary["overall"] == "fresh"


@pytest.mark.asyncio
async def test_reused_bundle_freshness_unavailable_kind_yields_unavailable() -> None:
    """ROB-278 Phase 2 — if any kind is 'unavailable', overall='unavailable'."""
    reused_summary = {
        "portfolio": {"status": "unavailable"},
        "journal": {"status": "fresh"},
    }
    ensure = _FakeEnsureService(
        _ensure_response(
            status="reused", freshness_summary=reused_summary, created=False
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    # 'unavailable' on a kind → published would be blocked; draft is allowed.
    response = await gen.generate(_make_request(status="draft"))
    assert response.snapshot_freshness_summary["overall"] == "unavailable"


@pytest.mark.asyncio
async def test_reused_bundle_freshness_no_kind_statuses_falls_back() -> None:
    """ROB-278 Phase 2 — when the stored summary has no per-kind statuses to
    derive from, fall back to the bundle-status mapping. For an unknown
    status (e.g. 'reused'), the safe default remains 'unavailable'.
    """
    ensure = _FakeEnsureService(
        _ensure_response(
            status="reused",
            # Non-empty (so the helper default doesn't substitute) but
            # contains no per-kind status entries to derive from.
            freshness_summary={"_meta": "no kind statuses present"},
            created=False,
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
    assert response.snapshot_freshness_summary["overall"] == "unavailable"


# ---------------------------------------------------------------------------
# ROB-278 Phase 2 — evidence-aware classifier downgrades.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_action_item_downgraded_to_review_when_no_quote_evidence() -> None:
    """ROB-278 Phase 2 — buy action item targeting a symbol whose symbol
    snapshot reports quote.status != 'ok' is downgraded to review with an
    explicit no-quote rationale. The generator wires symbol snapshots from
    the bundle into the classifier context."""
    from types import SimpleNamespace

    from app.schemas.investment_reports import IngestReportItem

    draft_items = [
        IngestReportItem(
            client_item_key="a-1",
            item_kind="action",
            symbol="005930",
            side="buy",
            intent="buy_review",
            rationale="buy thesis",
        )
    ]
    fake_bundle = SimpleNamespace(id=9999)
    fake_symbol_snapshot_item = SimpleNamespace(id=1, snapshot_id=20)
    fake_symbol_snapshot = SimpleNamespace(
        snapshot_kind="symbol",
        symbol="005930",
        payload_json={
            "symbol": "005930",
            "quote": {
                "status": "unavailable",
                "unavailable_reason": "session_closed",
            },
        },
    )
    fake_repo = _FakeSnapshotsRepository(
        bundle=fake_bundle,
        items=[(fake_symbol_snapshot_item, fake_symbol_snapshot)],
    )

    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )
    await gen.generate(_make_request(status="draft", items=draft_items))
    sent = ingest.calls[0]
    sent_item = sent.items[0]
    assert sent_item.operation == "review"
    assert "quote" in sent_item.rationale.lower()


@pytest.mark.asyncio
async def test_auto_emit_from_evidence_appends_review_items_from_bundle() -> None:
    """ROB-278 Phase 2 — auto_emit_from_evidence=True triggers the
    deterministic proposer; the resulting items are surfaced through
    ingest with operation='review' + apply_policy='requires_user_approval'.
    """
    from types import SimpleNamespace
    from uuid import uuid4

    fake_bundle = SimpleNamespace(id=7777)
    portfolio_snapshot = SimpleNamespace(
        snapshot_kind="portfolio",
        symbol=None,
        snapshot_uuid=uuid4(),
        payload_json={
            "primary_source": "kis",
            "holdings": [
                {
                    "ticker": "005930",
                    "quantity": 10,
                    "sellable_quantity": 8,
                    "source": "kis",
                    "market": "KR",
                }
            ],
            "reference_holdings": [],
            "count": 1,
            "market": "kr",
        },
    )
    symbol_snapshot = SimpleNamespace(
        snapshot_kind="symbol",
        symbol="005930",
        snapshot_uuid=uuid4(),
        payload_json={
            "symbol": "005930",
            "quote": {
                "status": "ok",
                "last_price": 70_000.0,
                "best_bid": 69_900.0,
                "best_ask": 70_100.0,
                "spread": 200.0,
                "spread_bps": 28.57,
                "bid_depth": 500.0,
                "ask_depth": 600.0,
                "venue": "krx",
            },
        },
    )

    fake_repo = _FakeSnapshotsRepository(
        bundle=fake_bundle,
        items=[
            (SimpleNamespace(id=1, snapshot_id=1), portfolio_snapshot),
            (SimpleNamespace(id=2, snapshot_id=2), symbol_snapshot),
        ],
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )
    await gen.generate(
        _make_request(status="draft", auto_emit_from_evidence=True, user_id=42)
    )
    sent = ingest.calls[0]
    auto_items = [
        i for i in sent.items if (i.client_item_key or "").startswith("auto-")
    ]
    assert auto_items, "expected at least one auto-emitted item"
    for item in auto_items:
        assert item.operation == "review"
        assert item.apply_policy == "requires_user_approval"


@pytest.mark.asyncio
async def test_auto_emit_from_evidence_respects_request_candidate_limit() -> None:
    """ROB-340 — generator forwards candidate_limit to the auto-emitter.

    This catches the partial-implementation regression where bundle collection
    honours candidate_limit but EvidenceAutoEmitter still emits its constructor
    default number of buy candidates.
    """
    from types import SimpleNamespace
    from uuid import uuid4

    fake_bundle = SimpleNamespace(id=8888)
    portfolio_snapshot = SimpleNamespace(
        snapshot_kind="portfolio",
        symbol=None,
        snapshot_uuid=uuid4(),
        payload_json={
            "primary_source": "kis",
            "holdings": [],
            "reference_holdings": [],
            "count": 0,
            "market": "kr",
        },
    )
    candidate_snapshot = SimpleNamespace(
        snapshot_kind="candidate_universe",
        symbol=None,
        snapshot_uuid=uuid4(),
        payload_json={
            "usefulness": "useful",
            "candidates": [
                {"symbol": "000660", "rank": 1, "score": 0.91},
                {"symbol": "035420", "rank": 2, "score": 0.82},
                {"symbol": "051910", "rank": 3, "score": 0.77},
            ],
        },
    )

    def _symbol_snapshot(symbol: str) -> SimpleNamespace:
        return SimpleNamespace(
            snapshot_kind="symbol",
            symbol=symbol,
            snapshot_uuid=uuid4(),
            payload_json={
                "symbol": symbol,
                "quote": {
                    "status": "ok",
                    "last_price": 100_000.0,
                    "best_bid": 99_900.0,
                    "best_ask": 100_100.0,
                    "spread_bps": 20.0,
                    "bid_depth": 100.0,
                    "ask_depth": 100.0,
                },
            },
        )

    fake_repo = _FakeSnapshotsRepository(
        bundle=fake_bundle,
        items=[
            (SimpleNamespace(id=1, snapshot_id=1), portfolio_snapshot),
            (SimpleNamespace(id=2, snapshot_id=2), candidate_snapshot),
            (SimpleNamespace(id=3, snapshot_id=3), _symbol_snapshot("000660")),
            (SimpleNamespace(id=4, snapshot_id=4), _symbol_snapshot("035420")),
            (SimpleNamespace(id=5, snapshot_id=5), _symbol_snapshot("051910")),
        ],
    )
    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )

    await gen.generate(
        _make_request(
            status="draft",
            auto_emit_from_evidence=True,
            candidate_limit=2,
            user_id=42,
        )
    )

    assert ensure.calls[0].candidate_limit == 2
    sent = ingest.calls[0]
    buy_items = [
        i for i in sent.items if (i.client_item_key or "").startswith("auto-buy-")
    ]
    assert [item.symbol for item in buy_items] == ["000660", "035420"]
    assert [item.priority for item in buy_items] == [1, 2]


@pytest.mark.asyncio
async def test_action_item_unchanged_when_quote_evidence_ok() -> None:
    """ROB-278 Phase 2 — when symbol snapshot reports quote.status='ok',
    the classifier does not downgrade the action item on quote grounds.
    (Other classifier paths — pending_orders, etc. — still apply.)"""
    from types import SimpleNamespace

    from app.schemas.investment_reports import IngestReportItem

    draft_items = [
        IngestReportItem(
            client_item_key="a-1",
            item_kind="action",
            symbol="005930",
            side="buy",
            intent="buy_review",
            rationale="buy thesis",
        )
    ]
    fake_bundle = SimpleNamespace(id=9999)
    fake_symbol_snapshot_item = SimpleNamespace(id=1, snapshot_id=20)
    fake_symbol_snapshot = SimpleNamespace(
        snapshot_kind="symbol",
        symbol="005930",
        payload_json={
            "symbol": "005930",
            "quote": {
                "status": "ok",
                "last_price": 70_000.0,
                "best_bid": 69_900.0,
                "best_ask": 70_100.0,
            },
        },
    )
    fake_pending_orders_item = SimpleNamespace(id=2, snapshot_id=30)
    fake_pending_orders_snapshot = SimpleNamespace(
        snapshot_kind="pending_orders",
        symbol=None,
        payload_json={"pending_orders": []},
    )
    fake_repo = _FakeSnapshotsRepository(
        bundle=fake_bundle,
        items=[
            (fake_symbol_snapshot_item, fake_symbol_snapshot),
            (fake_pending_orders_item, fake_pending_orders_snapshot),
        ],
    )

    ensure = _FakeEnsureService(_ensure_response())
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=fake_repo,
    )
    await gen.generate(_make_request(status="draft", items=draft_items))
    sent = ingest.calls[0]
    sent_item = sent.items[0]
    # No pending order matches, quote evidence is ok → caller's draft stands
    # untouched (no operation set by classifier).
    assert sent_item.operation is None
    assert "quote" not in (sent_item.rationale or "").lower()


@pytest.mark.asyncio
async def test_rob323_external_only_unavailable_does_not_block_published() -> None:
    """ROB-323 — toss/naver/browser all unavailable + every critical kind
    fresh + bundle_status='partial' → published report generates, overall is
    NOT 'unavailable', and no PublishBlockedByStaleGateError is raised."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "portfolio": {"status": "fresh"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
                "toss_remote_debug": {"status": "unavailable"},
                "naver_remote_debug": {"status": "unavailable"},
                "browser_probe": {"status": "unavailable"},
            },
            missing_sources=[
                "toss_remote_debug",
                "naver_remote_debug",
                "browser_probe",
            ],
        )
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    response = await gen.generate(_make_request(status="published"))
    assert response.snapshot_freshness_summary["overall"] == "partial"
    assert response.stale_gate["reject"] is False


@pytest.mark.asyncio
async def test_rob323_critical_unavailable_still_blocks_published() -> None:
    """ROB-323 — a CORE kind unavailable must keep failing closed even though
    the external sources are healthy."""
    ensure = _FakeEnsureService(
        _ensure_response(
            status="failed",
            freshness_summary={
                "portfolio": {"status": "unavailable"},
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
                "toss_remote_debug": {"status": "fresh"},
            },
            missing_sources=["portfolio"],
        )
    )
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=_FakeIngestionService(),
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    with pytest.raises(PublishBlockedByStaleGateError):
        await gen.generate(_make_request(status="published"))
