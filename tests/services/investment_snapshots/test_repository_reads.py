"""ROB-269 Phase 2 — Repository SELECT-only extensions."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 5, 19, 11, 11, 0, tzinfo=dt.UTC)


def _run_payload() -> SnapshotRunCreate:
    return SnapshotRunCreate(
        purpose="report_generation",
        market="kr",
        account_scope="kis_live",
        requested_by="user",
        policy_version="intraday_action_report_v1",
    )


def _snapshot_payload(
    run_uuid: uuid.UUID,
    *,
    symbol: str | None = None,
    kind: str = "symbol",
    price: float = 1.0,
) -> SnapshotCreate:
    return SnapshotCreate(
        run_uuid=run_uuid,
        snapshot_kind=kind,  # type: ignore[arg-type]
        market="kr",
        account_scope="kis_live",
        symbol=symbol,
        source_kind="kis_mcp",
        payload_json={"v": price, "u": str(uuid.uuid4())},
        as_of=_now(),
        freshness_status="fresh",
    )


def _bundle_payload(*, purpose: str = "kr_action_report") -> BundleCreate:
    return BundleCreate(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        as_of=_now(),
        status="complete",
    )


@pytest.mark.asyncio
async def test_find_latest_bundle_returns_none_when_none_exist(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    purpose = f"never_seen_{uuid.uuid4().hex[:8]}"
    found = await repo.find_latest_bundle(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
    )
    assert found is None


@pytest.mark.asyncio
async def test_find_latest_bundle_returns_most_recent_by_as_of(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    purpose = f"latest_test_{uuid.uuid4().hex[:8]}"

    older = BundleCreate(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        as_of=_now() - dt.timedelta(hours=1),
        status="complete",
    )
    newer = BundleCreate(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
        as_of=_now(),
        status="complete",
    )
    await repo.insert_bundle(older)
    newer_row = await repo.insert_bundle(newer)
    await db_session.commit()

    found = await repo.find_latest_bundle(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
    )
    assert found is not None
    assert found.bundle_uuid == newer_row.bundle_uuid


@pytest.mark.asyncio
async def test_find_latest_bundle_account_scope_null_match(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    purpose = f"no_scope_{uuid.uuid4().hex[:8]}"
    no_scope = BundleCreate(
        purpose=purpose,
        market="kr",
        account_scope=None,
        policy_version="intraday_action_report_v1",
        as_of=_now(),
        status="complete",
    )
    inserted = await repo.insert_bundle(no_scope)
    await db_session.commit()

    found = await repo.find_latest_bundle(
        purpose=purpose,
        market="kr",
        account_scope=None,
        policy_version="intraday_action_report_v1",
    )
    assert found is not None
    assert found.bundle_uuid == inserted.bundle_uuid

    # Different account scope should not match the NULL one.
    not_found = await repo.find_latest_bundle(
        purpose=purpose,
        market="kr",
        account_scope="kis_live",
        policy_version="intraday_action_report_v1",
    )
    assert not_found is None


@pytest.mark.asyncio
async def test_get_bundle_by_uuid_round_trip(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    bundle = await repo.insert_bundle(
        _bundle_payload(purpose=f"get_by_uuid_{uuid.uuid4().hex[:8]}")
    )
    await db_session.commit()

    fetched = await repo.get_bundle_by_uuid(bundle.bundle_uuid)
    assert fetched is not None
    assert fetched.id == bundle.id


@pytest.mark.asyncio
async def test_get_bundle_by_uuid_returns_none_when_missing(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    fetched = await repo.get_bundle_by_uuid(uuid.uuid4())
    assert fetched is None


@pytest.mark.asyncio
async def test_list_bundle_items_with_snapshots_orders_by_item_id(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    purpose = f"items_{uuid.uuid4().hex[:8]}"
    run = await repo.insert_run(_run_payload())
    bundle = await repo.insert_bundle(_bundle_payload(purpose=purpose))

    snap_a = await repo.insert_snapshot(
        _snapshot_payload(run.run_uuid, symbol=f"A{uuid.uuid4().hex[:6]}")
    )
    snap_b = await repo.insert_snapshot(
        _snapshot_payload(run.run_uuid, symbol=f"B{uuid.uuid4().hex[:6]}")
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap_a.snapshot_uuid, role="required"),
    )
    await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap_b.snapshot_uuid, role="optional"),
    )
    await db_session.commit()

    pairs = await repo.list_bundle_items_with_snapshots(bundle.id)
    assert len(pairs) == 2
    roles = [item.role for item, _snap in pairs]
    assert roles == ["required", "optional"]
    snap_uuids = [snap.snapshot_uuid for _item, snap in pairs]
    assert snap_uuids == [snap_a.snapshot_uuid, snap_b.snapshot_uuid]


@pytest.mark.asyncio
async def test_list_bundles_filters_by_purpose_and_status(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    purpose_a = f"purpose_a_{uuid.uuid4().hex[:8]}"
    purpose_b = f"purpose_b_{uuid.uuid4().hex[:8]}"
    await repo.insert_bundle(_bundle_payload(purpose=purpose_a))
    await repo.insert_bundle(_bundle_payload(purpose=purpose_b))
    await db_session.commit()

    only_a = await repo.list_bundles(purpose=purpose_a, limit=10)
    assert {b.purpose for b in only_a} == {purpose_a}

    only_complete_a = await repo.list_bundles(
        purpose=purpose_a, status="complete", limit=10
    )
    assert all(b.status == "complete" for b in only_complete_a)


@pytest.mark.asyncio
async def test_list_bundles_limit_clamped_to_100(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    # Limit clamp behavior — request 9999, repository must clamp to ≤ 100.
    rows = await repo.list_bundles(limit=9999)
    assert len(rows) <= 100


@pytest.mark.asyncio
async def test_list_snapshots_filters(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    unique_symbol = f"FIL{uuid.uuid4().hex[:6]}"
    await repo.insert_snapshot(
        _snapshot_payload(run.run_uuid, symbol=unique_symbol, kind="symbol")
    )
    await db_session.commit()

    by_symbol = await repo.list_snapshots(symbol=unique_symbol)
    assert len(by_symbol) >= 1
    assert all(s.symbol == unique_symbol for s in by_symbol)

    by_kind_filter = await repo.list_snapshots(
        symbol=unique_symbol, snapshot_kind="symbol"
    )
    assert len(by_kind_filter) == len(by_symbol)

    none_by_kind = await repo.list_snapshots(
        symbol=unique_symbol, snapshot_kind="news"
    )
    assert none_by_kind == []
