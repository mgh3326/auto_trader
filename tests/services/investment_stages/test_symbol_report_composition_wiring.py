"""ROB-301 T5 — composition consumes symbol intermediate reports.

* REGRESSION (CRITICAL): legacy composition (no symbol_intermediate_report_uuids)
  produces a metadata dict with NO symbol-report key — byte-identical to the
  pre-ROB-301 behavior; items pass through unchanged.
* Happy: referenced UUIDs are validated + attached under
  metadata["symbol_intermediate_report_uuids"] (D3 reference).
* Bad/missing UUID and cross-run membership are rejected (Codex #12).

Routing-level: ingestion + snapshots + symbol-reports repos are mocked.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.schemas.hermes_composition import (
    HermesCompositionIngestRequest,
    HermesCompositionResult,
)
from app.schemas.investment_reports import IngestReportItem
from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestError,
    HermesCompositionIngestService,
)


def _item() -> IngestReportItem:
    return IngestReportItem(
        client_item_key="auto-buy-BTC",
        item_kind="action",
        operation="review",
        symbol="BTC",
        side="buy",
        intent="buy_review",
        rationale="hermes rationale",
        apply_policy="requires_user_approval",
    )


def _composition(
    bundle_uuid, *, symbol_refs=None, run_uuid=None
) -> HermesCompositionResult:
    metadata = {}
    if run_uuid is not None:
        metadata["investment_stage_run_uuid"] = str(run_uuid)
    return HermesCompositionResult(
        snapshot_bundle_uuid=bundle_uuid,
        hermes_run_id="hermes-run-1",
        title="Hermes Advisory",
        summary="Synthesized via Hermes",
        items=[_item()],
        metadata=metadata,
        symbol_intermediate_report_uuids=symbol_refs or [],
    )


def _request(composition) -> HermesCompositionIngestRequest:
    return HermesCompositionIngestRequest(
        composition=composition,
        kst_date="2026-05-23",
        market="crypto",
        account_scope="upbit_live",
        status="draft",
    )


def _bundle(bundle_uuid) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        bundle_uuid=bundle_uuid,
        coverage_summary={"news": {"status": "fresh"}},
        freshness_summary={"overall": "fresh"},
        status="complete",
        market="crypto",
        account_scope="upbit_live",
        policy_version="intraday_action_report_v1",
    )


def _service(bundle_uuid, *, symbol_rows=None):
    snapshots = AsyncMock()
    snapshots.get_bundle_by_uuid.return_value = _bundle(bundle_uuid)
    ingestion = AsyncMock()
    ingestion.ingest.return_value = SimpleNamespace(report_uuid=uuid.uuid4())
    reports = AsyncMock()
    reports.get_by_uuids.return_value = symbol_rows or []
    svc = HermesCompositionIngestService(
        session=AsyncMock(),
        ingestion_service=ingestion,
        snapshots_repository=snapshots,
        symbol_reports_repository=reports,
    )
    return svc, ingestion


@pytest.mark.asyncio
async def test_regression_legacy_composition_has_no_symbol_ref_key():
    """CRITICAL: legacy composition (no refs) leaves metadata + items unchanged."""
    bundle_uuid = uuid.uuid4()
    svc, ingestion = _service(bundle_uuid)
    await svc.ingest_composition(_request(_composition(bundle_uuid)))
    call = ingestion.ingest.call_args.args[0]
    assert "symbol_intermediate_report_uuids" not in call.metadata
    assert call.metadata["hermes_composition"]["hermes_run_id"] == "hermes-run-1"
    assert len(call.items) == 1  # items pass through unchanged


@pytest.mark.asyncio
async def test_happy_attaches_validated_symbol_refs():
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    ref_a, ref_b = uuid.uuid4(), uuid.uuid4()
    rows = [
        SimpleNamespace(symbol_report_uuid=ref_a, run_uuid=run_uuid),
        SimpleNamespace(symbol_report_uuid=ref_b, run_uuid=run_uuid),
    ]
    svc, ingestion = _service(bundle_uuid, symbol_rows=rows)
    comp = _composition(bundle_uuid, symbol_refs=[ref_a, ref_b], run_uuid=run_uuid)
    await svc.ingest_composition(_request(comp))
    call = ingestion.ingest.call_args.args[0]
    assert call.metadata["symbol_intermediate_report_uuids"] == [str(ref_a), str(ref_b)]


@pytest.mark.asyncio
async def test_missing_symbol_ref_rejected():
    bundle_uuid = uuid.uuid4()
    ref = uuid.uuid4()
    svc, _ = _service(bundle_uuid, symbol_rows=[])  # none found
    comp = _composition(bundle_uuid, symbol_refs=[ref])
    with pytest.raises(
        HermesCompositionIngestError, match="unknown symbol intermediate"
    ):
        await svc.ingest_composition(_request(comp))


@pytest.mark.asyncio
async def test_cross_run_symbol_ref_rejected():
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    other_run = uuid.uuid4()
    ref = uuid.uuid4()
    rows = [SimpleNamespace(symbol_report_uuid=ref, run_uuid=other_run)]
    svc, _ = _service(bundle_uuid, symbol_rows=rows)
    comp = _composition(bundle_uuid, symbol_refs=[ref], run_uuid=run_uuid)
    with pytest.raises(
        HermesCompositionIngestError, match="do not belong to stage run"
    ):
        await svc.ingest_composition(_request(comp))
