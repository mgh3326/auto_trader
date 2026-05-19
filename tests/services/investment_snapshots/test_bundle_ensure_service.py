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
