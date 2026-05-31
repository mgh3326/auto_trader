"""ROB-380 — ensure_reusing_account_independent reuses live NULL-scope rows.

Distinct from ROB-373's test_cross_scope_reuse: here the mock path is given a
DIFFERENT account-independent payload than the live bundle, and we still expect
the mock bundle to cite the LIVE snapshot rows (because it LINKS, not collects).
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots_mcp import EnsureBundleRequest
from app.services.action_report.common.snapshot_bundle import (
    LiveBundleNotFoundForReuse,
    SnapshotBundleEnsureService,
)
from app.services.investment_snapshots.collectors import SnapshotCollectResult
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

_FIXED_NOW = dt.datetime(2025, 1, 15, 9, 0, tzinfo=dt.UTC)


def _frozen_clock():
    return lambda: _FIXED_NOW


def _manual(
    kind: str, *, account_scope: str | None, payload: dict
) -> SnapshotCollectResult:
    return SnapshotCollectResult(
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="us",  # type: ignore[arg-type]
        account_scope=account_scope,  # type: ignore[arg-type]
        source_kind="manual",
        payload_json=payload,
        as_of=_FIXED_NOW,
        freshness_status="fresh",
    )


async def _uuids_by_kind(repo, bundle_uuid):
    bundle = await repo.get_bundle_by_uuid(bundle_uuid)
    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    return {snap.snapshot_kind: snap.snapshot_uuid for _i, snap in pairs}


@pytest.mark.asyncio
async def test_reuse_links_live_independent_rows_even_when_mock_payload_differs(
    db_session,
) -> None:
    repo = InvestmentSnapshotsRepository(db_session)
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    purpose = f"rob380_reuse_{uuid.uuid4().hex[:8]}"

    # 1. Live bundle with account-independent + account-bound evidence.
    live = await svc.ensure(
        EnsureBundleRequest(
            purpose=purpose,
            market="us",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [
                    _manual(
                        "market",
                        account_scope="kis_live",
                        payload={"idx": "live-market"},
                    )
                ],
                "news": [
                    _manual(
                        "news", account_scope="kis_live", payload={"n": "live-news"}
                    )
                ],
                "portfolio": [
                    _manual(
                        "portfolio", account_scope="kis_live", payload={"p": "live"}
                    )
                ],
                "journal": [
                    _manual("journal", account_scope="kis_live", payload={"j": "live"})
                ],
                "watch_context": [
                    _manual(
                        "watch_context", account_scope="kis_live", payload={"w": "live"}
                    )
                ],
            },
        )
    )
    await db_session.commit()
    assert live.bundle_uuid is not None

    # 2. Mock bundle reuses live independent rows; account-bound supplied fresh
    #    for kis_mock. CRUCIALLY the mock's market/news manual data DIFFERS — it
    #    must be IGNORED in favor of linking the live rows.
    mock = await svc.ensure_reusing_account_independent(
        EnsureBundleRequest(
            purpose="mock_preview_report",
            market="us",
            account_scope="kis_mock",
            policy_version="intraday_action_report_v1",
            mode="ensure_fresh",
            manual_snapshots={
                "market": [
                    _manual(
                        "market",
                        account_scope="kis_mock",
                        payload={"idx": "MOCK-DIFFERENT"},
                    )
                ],
                "news": [
                    _manual(
                        "news",
                        account_scope="kis_mock",
                        payload={"n": "MOCK-DIFFERENT"},
                    )
                ],
                "portfolio": [
                    _manual(
                        "portfolio", account_scope="kis_mock", payload={"p": "mock"}
                    )
                ],
                "journal": [
                    _manual("journal", account_scope="kis_mock", payload={"j": "mock"})
                ],
                "watch_context": [
                    _manual(
                        "watch_context", account_scope="kis_mock", payload={"w": "mock"}
                    )
                ],
            },
        ),
        reuse_from_bundle_uuid=live.bundle_uuid,
    )
    await db_session.commit()
    assert mock.bundle_uuid is not None
    assert mock.bundle_uuid != live.bundle_uuid

    live_uuids = await _uuids_by_kind(repo, live.bundle_uuid)
    mock_uuids = await _uuids_by_kind(repo, mock.bundle_uuid)

    # Account-INDEPENDENT: SAME row, despite the mock's differing manual payload.
    for kind in ("market", "news"):
        assert mock_uuids[kind] == live_uuids[kind], (
            f"{kind} must be the reused live row, not a re-collected one"
        )

    # Account-BOUND: DIFFERENT rows per scope.
    for kind in ("portfolio", "journal", "watch_context"):
        assert mock_uuids[kind] != live_uuids[kind]


@pytest.mark.asyncio
async def test_reuse_raises_when_live_bundle_missing(db_session) -> None:
    svc = SnapshotBundleEnsureService(db_session, clock=_frozen_clock())
    with pytest.raises(LiveBundleNotFoundForReuse):
        await svc.ensure_reusing_account_independent(
            EnsureBundleRequest(
                purpose="mock_preview_report",
                market="us",
                account_scope="kis_mock",
                policy_version="intraday_action_report_v1",
                mode="ensure_fresh",
            ),
            reuse_from_bundle_uuid=uuid.uuid4(),
        )
