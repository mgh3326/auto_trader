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


def _item(**kwargs) -> IngestReportItem:
    base = {
        "client_item_key": "auto-buy-BTC",
        "item_kind": "action",
        "operation": "review",
        "symbol": "BTC",
        "side": "buy",
        "intent": "buy_review",
        "rationale": "hermes rationale",
        "apply_policy": "requires_user_approval",
    }
    base.update(kwargs)
    return IngestReportItem(**base)


def _composition(
    bundle_uuid, *, dimension_refs=None, run_uuid=None, items=None
) -> HermesCompositionResult:
    metadata = {}
    if run_uuid is not None:
        metadata["investment_stage_run_uuid"] = str(run_uuid)
    return HermesCompositionResult(
        snapshot_bundle_uuid=bundle_uuid,
        hermes_run_id="hermes-run-1",
        title="Hermes Advisory",
        summary="Synthesized via Hermes",
        items=items or [_item()],
        metadata=metadata,
        dimension_report_uuids=dimension_refs or [],
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


def _service(bundle_uuid, *, dimension_rows=None):
    snapshots = AsyncMock()
    snapshots.get_bundle_by_uuid.return_value = _bundle(bundle_uuid)
    ingestion = AsyncMock()
    ingestion.ingest.return_value = SimpleNamespace(report_uuid=uuid.uuid4())
    dimension_repo = AsyncMock()
    dimension_repo.get_by_uuids.return_value = dimension_rows or []
    svc = HermesCompositionIngestService(
        session=AsyncMock(),
        ingestion_service=ingestion,
        snapshots_repository=snapshots,
    )
    # inject mock dimension repo
    svc._dimension_reports = dimension_repo
    return svc, ingestion


@pytest.mark.asyncio
async def test_regression_legacy_composition_has_no_dimension_ref_key():
    bundle_uuid = uuid.uuid4()
    svc, ingestion = _service(bundle_uuid)
    await svc.ingest_composition(_request(_composition(bundle_uuid)))
    call = ingestion.ingest.call_args.args[0]
    assert "dimension_report_uuids" not in call.metadata


@pytest.mark.asyncio
async def test_happy_attaches_validated_dimension_refs():
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    ref = uuid.uuid4()
    rows = [SimpleNamespace(dimension_report_uuid=ref, run_uuid=run_uuid)]
    svc, ingestion = _service(bundle_uuid, dimension_rows=rows)
    comp = _composition(bundle_uuid, dimension_refs=[ref], run_uuid=run_uuid)
    await svc.ingest_composition(_request(comp))
    call = ingestion.ingest.call_args.args[0]
    assert call.metadata["dimension_report_uuids"] == [str(ref)]


@pytest.mark.asyncio
async def test_missing_dimension_ref_rejected():
    bundle_uuid = uuid.uuid4()
    ref = uuid.uuid4()
    svc, _ = _service(bundle_uuid, dimension_rows=[])
    comp = _composition(bundle_uuid, dimension_refs=[ref])
    with pytest.raises(
        HermesCompositionIngestError, match="dimension reports not found"
    ):
        await svc.ingest_composition(_request(comp))


@pytest.mark.asyncio
async def test_cross_run_dimension_ref_rejected():
    bundle_uuid = uuid.uuid4()
    run_uuid = uuid.uuid4()
    other_run = uuid.uuid4()
    ref = uuid.uuid4()
    rows = [SimpleNamespace(dimension_report_uuid=ref, run_uuid=other_run)]
    svc, _ = _service(bundle_uuid, dimension_rows=rows)
    comp = _composition(bundle_uuid, dimension_refs=[ref], run_uuid=run_uuid)
    with pytest.raises(
        HermesCompositionIngestError, match="dimension reports not in run"
    ):
        await svc.ingest_composition(_request(comp))


@pytest.mark.asyncio
async def test_items_persist_classification_fields():
    bundle_uuid = uuid.uuid4()
    su = uuid.uuid4()
    du = uuid.uuid4()
    svc, ingestion = _service(bundle_uuid)
    item = _item(
        decision_bucket="new_buy_candidate",
        cited_symbol_report_uuid=su,
        cited_dimension_report_uuids=[du],
    )
    comp = _composition(bundle_uuid, items=[item])
    await svc.ingest_composition(_request(comp))
    call = ingestion.ingest.call_args.args[0]
    assert len(call.items) == 1
    assert call.items[0].decision_bucket == "new_buy_candidate"
    assert call.items[0].cited_symbol_report_uuid == su
    assert call.items[0].cited_dimension_report_uuids == [du]
