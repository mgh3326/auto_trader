# tests/services/investment_snapshots/test_repository.py
import datetime as dt
import uuid

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
    SnapshotCreate,
    SnapshotRunCreate,
)
from app.services.action_report.common.canonicalize import canonical_payload_hash
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
        policy_snapshot_json={"portfolio": {"soft_ttl": 60, "hard_ttl": 300}},
        run_metadata={"local_smoke": True},
    )


def _snapshot_payload(run_uuid: uuid.UUID, *, symbol: str = "035420", price: float = 195000.0) -> SnapshotCreate:
    payload = {"symbol": symbol, "price": price}
    return SnapshotCreate(
        run_uuid=run_uuid,
        snapshot_kind="symbol",
        market="kr",
        account_scope="kis_live",
        symbol=symbol,
        source_kind="kis_mcp",
        payload_json=payload,
        as_of=_now(),
        freshness_status="fresh",
    )


@pytest.mark.asyncio
async def test_insert_run_returns_persisted_row(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    payload = _run_payload()
    payload.run_metadata = {"test": "test_insert_run_returns_persisted_row"}
    run = await repo.insert_run(payload)
    await db_session.commit()
    assert run.id > 0
    assert run.purpose == "report_generation"
    assert run.run_uuid is not None


@pytest.mark.asyncio
async def test_insert_snapshot_computes_canonical_hash_and_idempotency_key(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    symbol = f"S{uuid.uuid4().hex[:6]}"
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid, symbol=symbol))
    await db_session.commit()

    expected_hash = canonical_payload_hash({"symbol": symbol, "price": 195000.0})
    assert snap.canonical_payload_hash == expected_hash
    assert snap.idempotency_key.startswith(f"{run.run_uuid}:symbol:{symbol}:")
    assert snap.idempotency_key.endswith(expected_hash[:12])


@pytest.mark.asyncio
async def test_insert_snapshot_dedupes_identical_payload(db_session):
    """Same canonical payload → same row reused, second call returns existing."""
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    symbol = f"D{uuid.uuid4().hex[:6]}"
    a = await repo.insert_snapshot(_snapshot_payload(run.run_uuid, symbol=symbol))
    b = await repo.insert_snapshot(_snapshot_payload(run.run_uuid, symbol=symbol))
    await db_session.commit()
    assert a.id == b.id


@pytest.mark.asyncio
async def test_insert_bundle_and_link_items(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    symbol = f"B{uuid.uuid4().hex[:6]}"
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid, symbol=symbol))
    purpose = f"bundle_{uuid.uuid4().hex[:6]}"
    bundle = await repo.insert_bundle(
        BundleCreate(
            purpose=purpose,
            market="kr",
            account_scope="kis_live",
            policy_version="intraday_action_report_v1",
            as_of=_now(),
            status="complete",
            coverage_summary={"required": {symbol: "fresh"}},
            freshness_summary={symbol: {"as_of": _now().isoformat(), "status": "fresh"}},
        )
    )
    item = await repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snap.snapshot_uuid, role="required"),
    )
    await db_session.commit()
    assert item.bundle_id == bundle.id
    assert item.snapshot_id == snap.id
    assert item.role == "required"


@pytest.mark.asyncio
async def test_get_run_by_uuid_and_get_snapshot_by_uuid(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(_snapshot_payload(run.run_uuid))
    await db_session.commit()

    assert (await repo.get_run_by_uuid(run.run_uuid)).id == run.id
    assert (await repo.get_snapshot_by_uuid(snap.snapshot_uuid)).id == snap.id
    assert await repo.get_run_by_uuid(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_source_ref_domain_ref_round_trip(db_session):
    repo = InvestmentSnapshotsRepository(db_session)
    run = await repo.insert_run(_run_payload())
    snap = await repo.insert_snapshot(
        SnapshotCreate(
            run_uuid=run.run_uuid,
            snapshot_kind="candidate_universe",
            market="kr",
            source_kind="domain_ref",
            source_table="invest_screener_snapshots",
            source_id=42,
            source_uri="invest_screener_snapshots:42",
            payload_json={"ref_only": True},
            as_of=_now(),
            freshness_status="fresh",
        )
    )
    await db_session.commit()
    assert snap.source_table == "invest_screener_snapshots"
    assert snap.source_uri == "invest_screener_snapshots:42"
