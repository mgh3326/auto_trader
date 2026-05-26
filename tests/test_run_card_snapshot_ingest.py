"""ROB-329 — run-card → InvestmentSnapshot ingest + report-bundle visibility.

Uses the global ``db_session`` fixture (creates every table via
``Base.metadata.create_all``) because it touches both
``review.investment_snapshots`` and ``review.investment_reports``.

Producer→consumer path under test (decision #1 + #8):
  run_card.json → sanitize → InvestmentSnapshot(kind="validated_run_card")
  → linked into a report's snapshot bundle → visible via the existing
  report-centric snapshot read endpoints (no new endpoint).
"""

from __future__ import annotations

import datetime as dt
import json
import uuid as _uuid
from pathlib import Path

import pytest

from app.schemas.investment_snapshots import (
    BundleCreate,
    BundleItemCreate,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.repository import (
    InvestmentSnapshotsRepository,
)
from app.services.investment_snapshots.run_card_ingest import RunCardSnapshotIngestor

pytestmark = pytest.mark.usefixtures("investment_reports_cleanup_lock")

_NOW = dt.datetime(2026, 5, 26, 22, 44, tzinfo=dt.UTC)
_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "validated_run_card"
    / "run_card_insufficient_data.json"
)


def _load_fixture() -> dict:
    with _FIXTURE.open() as fh:
        return json.load(fh)


@pytest.mark.asyncio
async def test_ingest_creates_validated_run_card_snapshot(db_session):
    ingestor = RunCardSnapshotIngestor(InvestmentSnapshotsRepository(db_session))
    snapshot, citation = await ingestor.ingest(
        run_card_payload=_load_fixture(), market="crypto"
    )
    await db_session.commit()

    assert snapshot.snapshot_kind == "validated_run_card"
    assert snapshot.source_kind == "manual"
    assert snapshot.market == "crypto"
    assert snapshot.account_scope is None
    assert snapshot.symbol == "XRPUSDT"  # single-symbol run card

    # The persisted payload is the sanitized run card — strict-JSON safe.
    assert snapshot.payload_json["net_after_cost"]["profit_factor"] is None
    json.dumps(snapshot.payload_json, allow_nan=False)

    # Citation headline derived from the same payload.
    assert citation.verdict == "insufficient_data"
    assert citation.is_pass_stamp is False
    assert citation.trade_count == 2

    # Citeable by uuid.
    repo = InvestmentSnapshotsRepository(db_session)
    refetched = await repo.get_snapshot_by_uuid(snapshot.snapshot_uuid)
    assert refetched is not None
    assert refetched.snapshot_kind == "validated_run_card"


@pytest.mark.asyncio
async def test_ingested_snapshot_visible_in_report_bundle_read(db_session):
    snap_repo = InvestmentSnapshotsRepository(db_session)
    ingestor = RunCardSnapshotIngestor(snap_repo)
    snapshot, _citation = await ingestor.ingest(
        run_card_payload=_load_fixture(), market="crypto"
    )

    bundle = await snap_repo.insert_bundle(
        BundleCreate(
            purpose=f"rob329_{_uuid.uuid4().hex[:8]}",
            market="crypto",
            account_scope="upbit_live",
            policy_version="intraday_action_report_v1",
            as_of=_NOW,
            status="partial",
        )
    )
    await snap_repo.link_bundle_item(
        bundle_uuid=bundle.bundle_uuid,
        item=BundleItemCreate(snapshot_uuid=snapshot.snapshot_uuid, role="optional"),
    )

    report_repo = InvestmentReportsRepository(db_session)
    report = await report_repo.insert_report(
        report_uuid=_uuid.uuid4(),
        idempotency_key=f"k-{_uuid.uuid4().hex[:8]}",
        report_type="crypto_intraday",
        market="crypto",
        market_session="24x7",
        account_scope="upbit_live",
        execution_mode="advisory_only",
        created_by_profile="rob329-test",
        title="t",
        summary="s",
        snapshot_bundle_uuid=bundle.bundle_uuid,
        snapshot_policy_version="intraday_action_report_v1",
    )
    await db_session.commit()

    svc = InvestmentReportQueryService(db_session)

    # Bundle read lists the run-card snapshot as a member.
    response = await svc.get_report_snapshot_bundle(report.report_uuid)
    assert response is not None
    assert response.legacy_no_snapshot is False
    kinds = {item.snapshot_kind: item for item in response.items}
    assert "validated_run_card" in kinds
    assert kinds["validated_run_card"].role == "optional"

    # Detail read returns the sanitized payload (membership-checked).
    detail = await svc.get_report_snapshot_detail(
        report.report_uuid, snapshot.snapshot_uuid
    )
    assert detail is not None
    assert detail.snapshot_kind == "validated_run_card"
    assert detail.payload_json["net_after_cost"]["profit_factor"] is None
    assert detail.payload_json["verdict"] == "insufficient_data"
