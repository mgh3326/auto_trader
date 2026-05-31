"""ROB-373 — mock_preview report runner tests (Unit 2).

The runner projects a kis_live advisory report's items into a kis_mock /
mock_preview report, preserving ``cited_snapshot_uuids`` (provenance reuse) and
embedding a KIS-mock preview into each BUY action item's evidence. It writes
ONLY through ``InvestmentReportIngestionService`` — never the snapshot-backed
generator. Fail-closed: a missing/empty live report raises
``MockPreviewSourceMissing`` instead of producing an empty-success report.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.schemas.investment_snapshots_mcp import EnsureBundleResponse
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.mock_preview.runner import (
    MockPreviewReportRunner,
    MockPreviewSourceMissing,
)
from app.services.investment_reports.repository import InvestmentReportsRepository

# These tests use the global ``db_session`` fixture and seed
# ``review.investment_reports`` rows alongside snapshot/bundle rows. Under
# xdist that races with the helper ``session`` fixture's per-test
# ``TRUNCATE ... CASCADE`` on the report family (AccessExclusiveLock),
# producing an asyncpg ``DeadlockDetectedError`` (observed on main CI). The
# ``investment_reports_cleanup_lock`` fixture holds the same advisory lock the
# helper uses for the whole test body, serializing this file's report-table
# access against those truncates — the established pattern for cross-domain
# ``db_session`` tests (see test_hermes_roundtrip_smoke / test_run_card_snapshot_ingest).
pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")

_SEED_SNAPSHOT_UUID = uuid.UUID("11111111-1111-1111-1111-111111111111")


class _StubEnsureService:
    """Deterministic stand-in for SnapshotBundleEnsureService.

    The Unit-1 cross-scope reuse is exercised elsewhere; here we only need the
    runner to receive a ``bundle_uuid`` (or None) without driving the real
    collector registry over the network during the projection test.
    """

    async def ensure(self, request) -> EnsureBundleResponse:  # noqa: ANN001
        assert request.account_scope == "kis_mock"
        return EnsureBundleResponse(
            bundle_uuid=uuid.uuid4(),
            status="complete",
            created=True,
        )


@pytest_asyncio.fixture
async def seeded_live_report(db_session):
    """A kis_live advisory report with one BUY action item.

    The item carries ``cited_snapshot_uuids`` (the provenance signal the mock
    runner must preserve) and an evidence ``reference_price_usd`` so the BUY
    bridge can derive deterministic preview params. Seeded as ``draft`` to
    avoid the published-freshness DB CHECK (the ingestion service stamps a
    JSON-``null`` freshness summary that the published clause rejects); the
    runner reads items regardless of the source report's status.
    """
    request = IngestReportRequest(
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session="regular",
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="seed",
        title="t",
        summary="s",
        status="draft",
        generator_version="v2-snapshot-backed",
        kst_date="2026-05-30",
        items=[
            IngestReportItem(
                client_item_key="seed1",
                item_kind="action",
                side="buy",
                intent="buy_review",
                rationale="seed",
                symbol="AAPL",
                evidence_snapshot={"reference_price_usd": 200.0},
                max_action={"notional_usd": 50.0},
                cited_snapshot_uuids=[_SEED_SNAPSHOT_UUID],
            )
        ],
    )
    svc = InvestmentReportIngestionService(db_session)
    report = await svc.ingest(request)
    await db_session.flush()
    return report


@pytest.mark.asyncio
async def test_runner_projects_live_items_into_mock_preview_report(
    db_session, seeded_live_report
) -> None:
    runner = MockPreviewReportRunner(db_session, ensure_service=_StubEnsureService())
    report, _reused, count = await runner.run(
        live_report_uuid=seeded_live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    assert report.account_scope == "kis_mock"
    assert report.execution_mode == "mock_preview"
    assert report.status == "draft"
    assert count >= 1

    repo = InvestmentReportsRepository(db_session)
    mock_items = await repo.list_items_for_report(report.id)
    assert mock_items
    # provenance reuse: mock item cites the same snapshot uuids as the live item.
    assert mock_items[0].cited_snapshot_uuids
    assert uuid.UUID(str(mock_items[0].cited_snapshot_uuids[0])) == _SEED_SNAPSHOT_UUID
    # BUY action item carries an embedded (fail-closed) mock_preview block.
    assert "mock_preview" in mock_items[0].evidence_snapshot
    assert mock_items[0].evidence_snapshot["mock_preview"]["submit_enabled"] is False
    assert mock_items[0].apply_policy == "requires_user_approval"


class _RaisingBridge:
    """Stub bridge that always raises to simulate a transient KIS-mock failure."""

    async def preview(self, params):  # noqa: ANN001
        raise RuntimeError("simulated transient KIS-mock account read failure")


@pytest.mark.asyncio
async def test_runner_isolates_bridge_failure_as_per_item_error_sentinel(
    db_session, seeded_live_report
) -> None:
    """A bridge failure must become a per-item error sentinel, not abort the report.

    Invariants:
    - The run still returns a report with at least one item.
    - The BUY item's evidence_snapshot["mock_preview"]["status"] == "error".
    - submit_enabled is False (never enabled on error).
    - reason is the exception TYPE NAME only — no message/account values leaked.
    """
    runner = MockPreviewReportRunner(
        db_session,
        bridge=_RaisingBridge(),
        ensure_service=_StubEnsureService(),
    )
    report, _reused, count = await runner.run(
        live_report_uuid=seeded_live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )

    assert count >= 1

    repo = InvestmentReportsRepository(db_session)
    mock_items = await repo.list_items_for_report(report.id)
    assert mock_items

    buy_item = next(
        (i for i in mock_items if i.item_kind == "action" and i.side == "buy"),
        None,
    )
    assert buy_item is not None, "expected a BUY action item in projected report"

    mock_preview = buy_item.evidence_snapshot["mock_preview"]
    assert mock_preview["status"] == "error"
    assert mock_preview["submit_enabled"] is False

    # reason must be the exception type name only — no message/args that could
    # leak account identifiers or secret values.
    reason = mock_preview["reason"]
    assert reason == "RuntimeError"
    assert "simulated" not in reason  # message must NOT appear


@pytest.mark.asyncio
async def test_runner_fail_closed_when_live_report_missing(db_session) -> None:
    runner = MockPreviewReportRunner(db_session, ensure_service=_StubEnsureService())
    with pytest.raises(MockPreviewSourceMissing):
        await runner.run(
            live_report_uuid=uuid.uuid4(),
            market="us",
            market_session="regular",
            policy_version="intraday_action_report_v1",
            kst_date="2026-05-30",
            created_by_profile="schedule",
        )


@pytest.mark.asyncio
async def test_runner_fail_closed_when_live_report_has_no_items(db_session) -> None:
    """A live report that exists but has no items must raise MockPreviewSourceMissing."""
    # Seed a live report with zero items.
    request = IngestReportRequest(
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session="regular",
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="seed",
        title="empty-items report",
        summary="s",
        status="draft",
        generator_version="v2-snapshot-backed",
        kst_date="2026-05-30",
        items=[],
    )
    svc = InvestmentReportIngestionService(db_session)
    empty_report = await svc.ingest(request)
    await db_session.flush()

    runner = MockPreviewReportRunner(db_session, ensure_service=_StubEnsureService())
    with pytest.raises(MockPreviewSourceMissing):
        await runner.run(
            live_report_uuid=empty_report.report_uuid,
            market="us",
            market_session="regular",
            policy_version="intraday_action_report_v1",
            kst_date="2026-05-30",
            created_by_profile="schedule",
        )


@pytest.mark.asyncio
async def test_default_runner_wires_nonempty_production_registry(db_session) -> None:
    """ROB-379 smoke regression: an un-injected runner must use the production
    collector registry, not the empty Phase-2 default. With the empty default the
    kis_mock ensure collected nothing (bundle status=failed, all-unavailable) so
    the shared NULL-scope evidence was never reused. Guard the wiring directly."""
    runner = MockPreviewReportRunner(db_session)
    kinds = runner._ensure._collectors.list_kinds()
    assert kinds, "default runner ensure registry must not be empty"
    # account-independent evidence kinds must be collectable for cross-scope reuse
    assert {"market", "news"} <= kinds


@pytest.mark.asyncio
async def test_runner_reuses_live_bundle_account_independent_rows(db_session) -> None:
    """ROB-380 runtime invariant: when the live report carries a snapshot_bundle_uuid,
    the mock bundle cites the SAME account-independent snapshot rows (shared), while
    account-bound rows stay distinct per scope."""
    import datetime as _dt

    from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
    from app.services.action_report.common.snapshot_bundle import (
        SnapshotBundleEnsureService,
    )
    from app.services.investment_snapshots.collectors import SnapshotCollectResult
    from app.services.investment_snapshots.repository import (
        InvestmentSnapshotsRepository,
    )

    fixed_now = _dt.datetime(2025, 1, 15, 9, 0, tzinfo=_dt.UTC)

    def _manual(kind, scope, payload):
        return SnapshotCollectResult(
            snapshot_kind=kind,
            market="us",
            account_scope=scope,
            source_kind="manual",
            payload_json=payload,
            as_of=fixed_now,
            freshness_status="fresh",
        )

    snap_repo = InvestmentSnapshotsRepository(db_session)
    ensure = SnapshotBundleEnsureService(db_session, clock=lambda: fixed_now)

    # 1. Build a live bundle with account-independent + account-bound evidence.
    live_bundle = await ensure.ensure(
        EnsureBundleRequest(
            purpose="snapshot_backed_report",
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [_manual("market", "kis_live", {"m": "L"})],
                "news": [_manual("news", "kis_live", {"n": "L"})],
                "portfolio": [_manual("portfolio", "kis_live", {"p": "L"})],
                "journal": [_manual("journal", "kis_live", {"j": "L"})],
                "watch_context": [_manual("watch_context", "kis_live", {"w": "L"})],
            },
        )
    )
    await db_session.flush()
    assert live_bundle.bundle_uuid is not None

    # 2. Seed a live report that LINKS to that bundle (as the generator does).
    live_req = IngestReportRequest(
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session="regular",
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="seed",
        title="t",
        summary="s",
        status="draft",
        generator_version="v2-snapshot-backed",
        kst_date="2026-05-30",
        snapshot_bundle_uuid=live_bundle.bundle_uuid,
        items=[
            IngestReportItem(
                client_item_key="seed1",
                item_kind="action",
                side="buy",
                intent="buy_review",
                rationale="seed",
                symbol="AAPL",
                evidence_snapshot={"reference_price_usd": 200.0},
                max_action={"notional_usd": 50.0},
            )
        ],
    )
    live_report = await InvestmentReportIngestionService(db_session).ingest(live_req)
    await db_session.flush()

    # 3. Run the mock runner with a reuse-capable ensure service whose account-bound
    #    collection comes from manual snapshots (kis_mock).
    class _ReuseEnsure(SnapshotBundleEnsureService):
        async def ensure_reusing_account_independent(
            self, request, *, reuse_from_bundle_uuid
        ):  # noqa: ANN001
            request = request.model_copy(
                update={
                    "manual_snapshots": {
                        "portfolio": [_manual("portfolio", "kis_mock", {"p": "M"})],
                        "journal": [_manual("journal", "kis_mock", {"j": "M"})],
                        "watch_context": [
                            _manual("watch_context", "kis_mock", {"w": "M"})
                        ],
                    }
                }
            )
            return await super().ensure_reusing_account_independent(
                request, reuse_from_bundle_uuid=reuse_from_bundle_uuid
            )

    runner = MockPreviewReportRunner(
        db_session, ensure_service=_ReuseEnsure(db_session, clock=lambda: fixed_now)
    )
    mock_report, _reused, _count = await runner.run(
        live_report_uuid=live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    await db_session.flush()

    # 4. Assert shared independent rows, distinct account-bound rows.
    async def _by_kind(bundle_uuid):
        b = await snap_repo.get_bundle_by_uuid(bundle_uuid)
        pairs = await snap_repo.list_bundle_items_with_snapshots(b.id)
        return {s.snapshot_kind: s.snapshot_uuid for _i, s in pairs}

    live_uuids = await _by_kind(live_bundle.bundle_uuid)
    mock_uuids = await _by_kind(mock_report.snapshot_bundle_uuid)

    assert mock_uuids["market"] == live_uuids["market"]
    assert mock_uuids["news"] == live_uuids["news"]
    assert mock_uuids["portfolio"] != live_uuids["portfolio"]


@pytest.mark.asyncio
async def test_runner_fallback_when_live_bundle_uuid_absent(
    db_session, seeded_live_report
) -> None:
    """When live.snapshot_bundle_uuid is None, it should fall back to calling ensure()."""

    class _MockEnsure:
        called = False

        async def ensure(self, request) -> EnsureBundleResponse:
            self.called = True
            return EnsureBundleResponse(
                bundle_uuid=uuid.uuid4(),
                status="complete",
                created=True,
            )

    mock_ensure = _MockEnsure()
    runner = MockPreviewReportRunner(db_session, ensure_service=mock_ensure)
    report, _reused, count = await runner.run(
        live_report_uuid=seeded_live_report.report_uuid,
        market="us",
        market_session="regular",
        policy_version="intraday_action_report_v1",
        kst_date="2026-05-30",
        created_by_profile="schedule",
    )
    assert mock_ensure.called
    assert report.snapshot_bundle_uuid is not None
