import pytest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.services.action_report.snapshot_backed.generator import SnapshotBackedReportGenerator
from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


@pytest.mark.asyncio
async def test_auto_emit_threads_budget(monkeypatch):
    captured = {}

    class FakeEmitter:
        def __init__(self, **_kw):
            pass

        def propose(self, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(
        "app.services.action_report.snapshot_backed.auto_emit.EvidenceAutoEmitter",
        FakeEmitter,
    )

    mock_session = AsyncMock()
    mock_repo = MagicMock()
    mock_bundle = MagicMock()
    mock_bundle.id = 123
    mock_repo.get_bundle_by_uuid = AsyncMock(return_value=mock_bundle)
    mock_repo.list_bundle_items_with_snapshots = AsyncMock(return_value=[])

    generator = SnapshotBackedReportGenerator(
        session=mock_session,
        snapshots_repository=mock_repo
    )

    request = ReportGenerationRequest(
        market="us",
        account_scope="kis_live",
        created_by_profile="operator",
        title="Test Budget",
        summary="Test",
        kst_date="2026-06-09",
        budget_basis="krw_orderable_reference",
        operator_budget_override_usd=500.0,
    )

    bundle_uuid = uuid4()
    await generator._auto_emit_items_from_bundle(
        bundle_uuid=bundle_uuid,
        request=request,
    )

    assert captured.get("budget_basis") == "krw_orderable_reference"
    assert captured.get("operator_budget_override_usd") == 500.0
