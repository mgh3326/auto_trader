"""Unit tests for the Hermes composition ingest service (ROB-287)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.schemas.hermes_composition import (
    HERMES_COMPOSITION_VERSION,
    HermesCompositionIngestRequest,
    HermesCompositionResult,
)
from app.schemas.investment_reports import IngestReportItem
from app.services.investment_stages.hermes_ingest import (
    HermesCompositionIngestError,
    HermesCompositionIngestService,
)


def _make_item(
    *,
    client_item_key: str,
    operation: str = "review",
    apply_policy: str = "requires_user_approval",
    item_kind: str = "action",
    intent: str = "buy_review",
    symbol: str | None = "BTC",
    side: str | None = "buy",
) -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind=item_kind,
        operation=operation,
        symbol=symbol,
        side=side,
        intent=intent,
        rationale="hermes-produced rationale",
        apply_policy=apply_policy,
    )


def _make_composition(
    *, bundle_uuid: uuid.UUID, items: list[IngestReportItem] | None = None
) -> HermesCompositionResult:
    return HermesCompositionResult(
        snapshot_bundle_uuid=bundle_uuid,
        hermes_run_id="hermes-run-1",
        title="Hermes Advisory",
        summary="Synthesized via Hermes",
        items=items
        if items is not None
        else [_make_item(client_item_key="auto-buy-BTC")],
    )


def _make_request(
    composition: HermesCompositionResult,
) -> HermesCompositionIngestRequest:
    return HermesCompositionIngestRequest(
        composition=composition,
        kst_date="2026-05-20",
        market="crypto",
        account_scope="upbit_live",
        status="draft",
    )


def _make_bundle(bundle_uuid: uuid.UUID) -> SimpleNamespace:
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


@pytest.mark.asyncio
async def test_ingest_composition_routes_through_ingestion_service() -> None:
    bundle_uuid = uuid.uuid4()
    composition = _make_composition(bundle_uuid=bundle_uuid)
    request = _make_request(composition)

    snapshots_repo = AsyncMock()
    snapshots_repo.get_bundle_by_uuid.return_value = _make_bundle(bundle_uuid)

    ingestion_service = AsyncMock()
    expected_report = SimpleNamespace(report_uuid=uuid.uuid4())
    ingestion_service.ingest.return_value = expected_report

    service = HermesCompositionIngestService(
        session=AsyncMock(),
        ingestion_service=ingestion_service,
        snapshots_repository=snapshots_repo,
    )

    result = await service.ingest_composition(request)
    assert result is expected_report

    ingestion_service.ingest.assert_awaited_once()
    call = ingestion_service.ingest.call_args.args[0]
    assert call.snapshot_bundle_uuid == bundle_uuid
    assert call.generator_version == HERMES_COMPOSITION_VERSION
    assert call.metadata["hermes_composition"]["hermes_run_id"] == "hermes-run-1"
    assert call.snapshot_freshness_summary == {"overall": "fresh"}
    assert call.kst_date == "2026-05-20"
    assert len(call.items) == 1
    assert call.items[0].operation == "review"
    assert call.items[0].apply_policy == "requires_user_approval"


@pytest.mark.asyncio
async def test_ingest_composition_raises_when_bundle_missing() -> None:
    bundle_uuid = uuid.uuid4()
    composition = _make_composition(bundle_uuid=bundle_uuid)
    request = _make_request(composition)

    snapshots_repo = AsyncMock()
    snapshots_repo.get_bundle_by_uuid.return_value = None

    service = HermesCompositionIngestService(
        session=AsyncMock(),
        ingestion_service=AsyncMock(),
        snapshots_repository=snapshots_repo,
    )

    with pytest.raises(HermesCompositionIngestError):
        await service.ingest_composition(request)


def test_composition_rejects_create_operation_items() -> None:
    """Composition validator: only review/cancel/keep with requires_user_approval."""
    bundle_uuid = uuid.uuid4()
    with pytest.raises(ValidationError) as excinfo:
        _make_composition(
            bundle_uuid=bundle_uuid,
            items=[
                _make_item(
                    client_item_key="bad-create",
                    operation="create",
                ),
            ],
        )
    assert "advisory-only" in str(excinfo.value)


def test_composition_rejects_missing_apply_policy() -> None:
    bundle_uuid = uuid.uuid4()
    with pytest.raises(ValidationError) as excinfo:
        _make_composition(
            bundle_uuid=bundle_uuid,
            items=[
                _make_item(
                    client_item_key="bad-policy",
                    apply_policy="auto_apply",
                ),
            ],
        )
    assert "requires_user_approval" in str(excinfo.value)


def test_composition_accepts_no_items_partial_data_case() -> None:
    """Partial data: Hermes may legitimately emit zero items when bundle
    is too thin. The validator must not insist on at least one item."""
    bundle_uuid = uuid.uuid4()
    comp = _make_composition(bundle_uuid=bundle_uuid, items=[])
    assert comp.items == []
    assert comp.snapshot_bundle_uuid == bundle_uuid
