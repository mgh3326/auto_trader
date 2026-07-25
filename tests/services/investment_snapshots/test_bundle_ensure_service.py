"""ROB-269 Phase 2 — SnapshotBundleEnsureService (core)."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from app.models.investment_snapshots import (
    InvestmentSnapshotRun,
)
from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectorRegistry,
    SnapshotCollectResult,
)

_FIXED_NOW = dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


def _frozen_clock():
    return lambda: _FIXED_NOW


def _manual_snapshot(
    kind: str,
    *,
    as_of: dt.datetime | None = None,
    market: str = "kr",
    account_scope: str | None = "kis_live",
    payload: dict | None = None,
) -> SnapshotCollectResult:
    return SnapshotCollectResult(
        snapshot_kind=kind,  # type: ignore[arg-type]
        market=market,  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json=payload or {"k": kind, "u": str(uuid.uuid4())},
        as_of=as_of or _FIXED_NOW,
        freshness_status="fresh",
    )


def _all_required_manual_snapshots() -> dict[str, list[SnapshotCollectResult]]:
    return {
        "portfolio": [_manual_snapshot("portfolio")],
        "journal": [_manual_snapshot("journal")],
        "watch_context": [_manual_snapshot("watch_context")],
        "market": [_manual_snapshot("market", account_scope=None)],
    }


def _unique_purpose(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


@pytest.mark.asyncio
async def test_reuse_only_with_no_fresh_bundle_returns_failed_without_db_write(
    db_session,
):
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("reuse_only_miss"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="reuse_only",
        )
    )
    assert response.status == "failed"
    assert response.bundle_uuid is None
    assert response.created is False
    assert response.run_uuid is None
    assert any("reuse_only" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_ensure_fresh_with_empty_registry_and_no_manual_returns_failed(
    db_session,
):
    """All required kinds unavailable → bundle status=failed, but bundle is created."""
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("empty_collectors"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
        )
    )
    await db_session.commit()

    assert response.status == "failed"
    assert response.bundle_uuid is not None
    assert response.created is True
    assert response.run_uuid is not None
    assert set(response.missing_sources) >= {
        "portfolio",
        "journal",
        "watch_context",
        "market",
    }


@pytest.mark.asyncio
async def test_ensure_fresh_with_all_required_manual_returns_complete(db_session):
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("all_required"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()

    assert response.status == "complete"
    assert response.created is True
    assert response.bundle_uuid is not None
    # All 4 required kinds present in coverage_summary.required as 'fresh'.
    assert set(response.coverage_summary["required"]) == {
        "portfolio",
        "journal",
        "watch_context",
        "market",
    }
    assert all(s == "fresh" for s in response.coverage_summary["required"].values())


@pytest.mark.asyncio
async def test_ensure_fresh_with_required_complete_and_one_optional_failing_is_partial(
    db_session,
):
    """All required succeed; one optional collector raises → bundle=partial."""

    class _BrokenNewsCollector:
        snapshot_kind = "news"

        async def collect(self, request: CollectorRequest):
            raise RuntimeError("news source unreachable")

    registry = SnapshotCollectorRegistry()
    registry.register(_BrokenNewsCollector())

    svc = SnapshotBundleEnsureService(
        db_session, collectors=registry, clock=_frozen_clock()
    )
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("optional_fail"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()

    assert response.status == "partial"
    # The optional 'news' kind appears as unavailable, others stay fresh.
    assert response.coverage_summary["optional"]["news"] == "unavailable"
    assert any("news" in w and "RuntimeError" in w for w in response.warnings)


@pytest.mark.asyncio
async def test_ensure_fresh_with_hard_stale_required_returns_stale_fallback(
    db_session,
):
    """Required portfolio with very old as_of → hard_stale → bundle=stale_fallback."""
    old_as_of = _FIXED_NOW - dt.timedelta(hours=1)  # > portfolio hard TTL 300s
    manual = _all_required_manual_snapshots()
    manual["portfolio"] = [_manual_snapshot("portfolio", as_of=old_as_of)]

    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("stale_fallback"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=manual,
        )
    )
    await db_session.commit()

    assert response.status == "stale_fallback"
    assert response.coverage_summary["required"]["portfolio"] == "hard_stale"


@pytest.mark.asyncio
async def test_ensure_fresh_second_call_within_soft_ttl_reuses_bundle(db_session):
    purpose = _unique_purpose("reuse_round_trip")
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())

    first = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()
    assert first.status == "complete"

    second = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()
    assert second.status == "reused"
    assert second.bundle_uuid == first.bundle_uuid
    assert second.created is False
    assert second.run_uuid is None


@pytest.mark.asyncio
async def test_ensure_fresh_does_not_reuse_fresh_failed_bundle(db_session):
    """ROB-314 regression: a time-fresh but status=failed bundle (e.g. built
    earlier without user_id / with the empty default registry) must NOT
    short-circuit collection under ensure_fresh. The second call re-collects
    and produces a real bundle instead of returning status='reused'.

    The clock advances 10s between calls (well within the 180s soft TTL) so
    the failed bundle is still classified 'fresh' — exactly the scenario the
    fix targets — while the distinct ``as_of`` lets the re-collection persist a
    new bundle row rather than colliding on the deterministic idempotency key.
    """
    purpose = _unique_purpose("failed_no_reuse")
    clock_now = {"t": _FIXED_NOW}
    svc = SnapshotBundleEnsureService(db_session, clock=lambda: clock_now["t"])

    # First call: empty registry, no manual data → failed bundle, still fresh.
    first = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
        )
    )
    await db_session.commit()
    assert first.status == "failed"
    assert first.bundle_uuid is not None

    # Second call within soft TTL, now WITH required data available. Must not
    # reuse the failed bundle; must re-collect into a new bundle.
    clock_now["t"] = _FIXED_NOW + dt.timedelta(seconds=10)
    second = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()
    assert second.status == "complete"
    assert second.created is True
    assert second.bundle_uuid != first.bundle_uuid


@pytest.mark.asyncio
async def test_ensure_fresh_records_user_id_in_run_metadata(db_session):
    """ROB-314: user_id is captured in run audit metadata so a later smoke can
    confirm which user scope a bundle was collected for."""
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("user_scope_audit"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
            user_id=42,
        )
    )
    await db_session.commit()

    run = await db_session.scalar(
        sa.select(InvestmentSnapshotRun).where(
            InvestmentSnapshotRun.run_uuid == response.run_uuid
        )
    )
    assert run is not None
    assert run.run_metadata["ensure_request"]["user_id"] == 42


@pytest.mark.asyncio
async def test_ensure_fresh_writes_run_with_frozen_policy_snapshot(db_session):
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("policy_freeze"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=_all_required_manual_snapshots(),
        )
    )
    await db_session.commit()

    run = await db_session.scalar(
        sa.select(InvestmentSnapshotRun).where(
            InvestmentSnapshotRun.run_uuid == response.run_uuid
        )
    )
    assert run is not None
    snap = run.policy_snapshot_json
    assert snap["policy_version"] == "intraday_action_report_v1"
    assert snap["bundle_ttl_seconds"] == {"soft": 180, "hard": 300}
    portfolio_entry = next(
        k for k in snap["kinds"] if k["snapshot_kind"] == "portfolio"
    )
    assert portfolio_entry["required"] is True


@pytest.mark.asyncio
async def test_ensure_fresh_uses_collector_registry_when_no_manual(db_session):
    """When a kind has no manual_snapshot but a collector is registered, the
    collector populates the bundle."""

    class _FakePortfolioCollector:
        snapshot_kind = "portfolio"

        async def collect(self, request: CollectorRequest):
            return [
                SnapshotCollectResult(
                    snapshot_kind="portfolio",
                    market=request.market,
                    account_scope=request.account_scope,
                    source_kind="manual",
                    payload_json={"cash_krw": 999_999, "u": str(uuid.uuid4())},
                    as_of=_FIXED_NOW,
                    freshness_status="fresh",
                )
            ]

    registry = SnapshotCollectorRegistry()
    registry.register(_FakePortfolioCollector())

    manual = _all_required_manual_snapshots()
    # Strip portfolio from manual — collector should provide it.
    del manual["portfolio"]

    svc = SnapshotBundleEnsureService(
        db_session, collectors=registry, clock=_frozen_clock()
    )
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("collector_only"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=manual,
        )
    )
    await db_session.commit()

    assert response.status == "complete"
    assert response.coverage_summary["required"]["portfolio"] == "fresh"


@pytest.mark.asyncio
async def test_ensure_surfaces_collector_reason_code_in_freshness_summary(db_session):
    """ROB-318 Slice 1 — a collector's reason_code/reason for a degraded kind is
    surfaced in freshness_summary[kind] (previously only the bare status survived)."""

    class _FailClosedPortfolioCollector:
        snapshot_kind = "portfolio"

        async def collect(self, request: CollectorRequest):
            return [
                SnapshotCollectResult(
                    snapshot_kind="portfolio",
                    market=request.market,
                    account_scope=request.account_scope,
                    source_kind="manual",
                    payload_json={},
                    errors_json={
                        "reason_code": "user_id_missing",
                        "reason": (
                            "kis_live portfolio requires explicit user_id; "
                            "none supplied"
                        ),
                    },
                    as_of=_FIXED_NOW,
                    freshness_status="unavailable",
                )
            ]

    registry = SnapshotCollectorRegistry()
    registry.register(_FailClosedPortfolioCollector())
    manual = _all_required_manual_snapshots()
    del manual["portfolio"]

    svc = SnapshotBundleEnsureService(
        db_session, collectors=registry, clock=_frozen_clock()
    )
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("reason_code"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=manual,
        )
    )
    await db_session.commit()

    pf = response.freshness_summary["portfolio"]
    assert pf["status"] == "unavailable"
    assert pf["reason_code"] == "user_id_missing"
    assert "user_id" in pf["reason"]


@pytest.mark.asyncio
async def test_ensure_fresh_classifies_collector_results_against_post_collect_clock(
    db_session,
):
    """Collector results can be stamped after ensure() starts.

    A live collector may spend several seconds reading KIS before returning a
    SnapshotCollectResult. Freshness classification must compare that result to
    the post-collection clock, not to the start-of-ensure reuse-gate timestamp.
    """

    collected_as_of = _FIXED_NOW + dt.timedelta(seconds=6)

    class _DelayedPortfolioCollector:
        snapshot_kind = "portfolio"

        async def collect(self, request: CollectorRequest):
            return [
                SnapshotCollectResult(
                    snapshot_kind="portfolio",
                    market=request.market,
                    account_scope=request.account_scope,
                    source_kind="manual",
                    payload_json={"cash_krw": 1_000_000, "u": str(uuid.uuid4())},
                    as_of=collected_as_of,
                    freshness_status="fresh",
                )
            ]

    calls = 0

    def clock():
        nonlocal calls
        calls += 1
        if calls == 1:
            return _FIXED_NOW
        return collected_as_of

    registry = SnapshotCollectorRegistry()
    registry.register(_DelayedPortfolioCollector())

    manual = _all_required_manual_snapshots()
    del manual["portfolio"]

    svc = SnapshotBundleEnsureService(db_session, collectors=registry, clock=clock)
    response = await svc.ensure(
        EnsureBundleRequest(
            purpose=_unique_purpose("post_collect_clock"),
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            manual_snapshots=manual,  # type: ignore[arg-type]
        )
    )
    await db_session.commit()

    assert response.status == "complete"
    assert response.coverage_summary["required"]["portfolio"] == "fresh"
    assert (
        response.freshness_summary["portfolio"]["as_of"] == collected_as_of.isoformat()
    )


@pytest.mark.asyncio
async def test_create_new_ignores_fresh_prior_bundle_and_collects(db_session):
    purpose = "analysis_recheck"
    prior_service = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    prior = await prior_service.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="analysis_snapshot_bundle_v1",
            mode="ensure_fresh",
            symbols=["005930"],
            manual_snapshots={
                "llm_input_frozen": [
                    _manual_snapshot("llm_input_frozen", payload={"version": "prior"})
                ]
            },
            requested_by="claude_code",
            user_id=7,
        )
    )
    await db_session.commit()

    class _FakeFrozenInputCollector:
        snapshot_kind = "llm_input_frozen"

        def __init__(self) -> None:
            self.calls = 0

        async def collect(self, request: CollectorRequest):
            self.calls += 1
            return [
                SnapshotCollectResult(
                    snapshot_kind="llm_input_frozen",
                    market=request.market,
                    account_scope=request.account_scope,
                    source_kind="manual",
                    payload_json={"version": "new"},
                    as_of=_FIXED_NOW + dt.timedelta(seconds=1),
                    freshness_status="fresh",
                )
            ]

    collector = _FakeFrozenInputCollector()
    registry = SnapshotCollectorRegistry()
    registry.register(collector)
    service = SnapshotBundleEnsureService(
        db_session,
        collectors=registry,
        clock=lambda: _FIXED_NOW + dt.timedelta(seconds=1),
    )
    request = EnsureBundleRequest(
        purpose="analysis_recheck",
        market="kr",
        account_scope="kis_live",
        policy_version="analysis_snapshot_bundle_v1",
        mode="create_new",
        symbols=["005930"],
        requested_by="claude_code",
        user_id=7,
    )
    response = await service.ensure(request)
    assert response.created is True
    assert response.bundle_uuid != prior.bundle_uuid
    assert collector.calls == 1
