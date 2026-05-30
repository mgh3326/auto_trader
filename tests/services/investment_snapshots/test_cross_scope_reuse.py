"""ROB-373 Task 3 — Cross-scope evidence reuse integration test (Unit 1 / AC).

Proves that when a kis_live bundle and a kis_mock bundle are ensured for the
same US market/date with identical account-independent payloads:

* market / news / candidate_universe snapshots are the SAME row (shared)
* portfolio / journal / watch_context are DIFFERENT rows (scope-separated)
* bundle_uuids are distinct (scope is part of bundle identity)
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.collectors import SnapshotCollectResult
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_FIXED_NOW = dt.datetime(2025, 1, 15, 9, 0, tzinfo=dt.timezone.utc)  # well in the past


def _frozen_clock():
    return lambda: _FIXED_NOW


def _manual(kind: str, *, account_scope: str | None) -> SnapshotCollectResult:
    # Identical payload across both ensure calls so dedup can match.
    return SnapshotCollectResult(
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json={"k": kind, "stable": "evidence"},
        as_of=_FIXED_NOW,
        freshness_status="fresh",
    )


def _manual_for(scope: str) -> dict[str, list[SnapshotCollectResult]]:
    # account-independent kinds: identical payload (dedup will share them).
    # account-bound kinds: include scope in payload so they are distinct rows.
    return {
        # Required account-independent
        "market": [_manual("market", account_scope=scope)],
        # Required account-bound (include scope so payloads differ → distinct rows)
        "portfolio": [
            SnapshotCollectResult(
                snapshot_kind="portfolio",  # type: ignore[arg-type]
                market="us",  # type: ignore[arg-type]
                account_scope=scope,  # type: ignore[arg-type]
                source_kind="manual",
                payload_json={"k": "portfolio", "scope": scope},
                as_of=_FIXED_NOW,
                freshness_status="fresh",
            )
        ],
        "journal": [
            SnapshotCollectResult(
                snapshot_kind="journal",  # type: ignore[arg-type]
                market="us",  # type: ignore[arg-type]
                account_scope=scope,  # type: ignore[arg-type]
                source_kind="manual",
                payload_json={"k": "journal", "scope": scope},
                as_of=_FIXED_NOW,
                freshness_status="fresh",
            )
        ],
        "watch_context": [
            SnapshotCollectResult(
                snapshot_kind="watch_context",  # type: ignore[arg-type]
                market="us",  # type: ignore[arg-type]
                account_scope=scope,  # type: ignore[arg-type]
                source_kind="manual",
                payload_json={"k": "watch_context", "scope": scope},
                as_of=_FIXED_NOW,
                freshness_status="fresh",
            )
        ],
        # Optional account-independent (for reuse assertion)
        "news": [_manual("news", account_scope=scope)],
        "candidate_universe": [_manual("candidate_universe", account_scope=scope)],
    }


async def _uuids_by_kind(
    repo: InvestmentSnapshotsRepository, bundle_uuid: uuid.UUID
) -> dict[str, uuid.UUID]:
    bundle = await repo.get_bundle_by_uuid(bundle_uuid)
    assert bundle is not None, f"bundle not found: {bundle_uuid}"
    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    return {snap.snapshot_kind: snap.snapshot_uuid for _item, snap in pairs}


@pytest.mark.asyncio
async def test_independent_evidence_shared_account_bound_separated(db_session) -> None:
    """AC: account-independent kinds share one snapshot row across scopes;
    account-bound kinds produce separate rows per scope."""
    repo = InvestmentSnapshotsRepository(db_session)
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    purpose = f"rob373_reuse_{uuid.uuid4().hex[:8]}"

    live = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots=_manual_for("kis_live"),
        )
    )
    await db_session.commit()

    mock = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="us",
            account_scope="kis_mock",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots=_manual_for("kis_mock"),
        )
    )
    await db_session.commit()

    # Both bundles must have been created (not reused — different account_scope).
    assert live.bundle_uuid is not None, f"live bundle failed: {live}"
    assert mock.bundle_uuid is not None, f"mock bundle failed: {mock}"

    # Distinct bundles (scope is part of bundle identity).
    assert live.bundle_uuid != mock.bundle_uuid

    live_uuids = await _uuids_by_kind(repo, live.bundle_uuid)
    mock_uuids = await _uuids_by_kind(repo, mock.bundle_uuid)

    # Account-INDEPENDENT evidence is the SAME row in both bundles.
    for kind in ("market", "news", "candidate_universe"):
        assert kind in live_uuids, f"{kind} missing from live bundle items"
        assert kind in mock_uuids, f"{kind} missing from mock bundle items"
        assert live_uuids[kind] == mock_uuids[kind], (
            f"{kind} should be a shared snapshot row across scopes, "
            f"but got live={live_uuids[kind]} vs mock={mock_uuids[kind]}"
        )

    # Account-BOUND evidence is a DIFFERENT row per scope.
    for kind in ("portfolio", "journal", "watch_context"):
        assert kind in live_uuids, f"{kind} missing from live bundle items"
        assert kind in mock_uuids, f"{kind} missing from mock bundle items"
        assert live_uuids[kind] != mock_uuids[kind], (
            f"{kind} should be scope-separated snapshots, "
            f"but got the same uuid={live_uuids[kind]}"
        )
