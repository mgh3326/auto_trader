from unittest.mock import AsyncMock

import pytest

from app.services.action_report.snapshot_backed.generator import (
    SnapshotBackedReportGenerator,
)
from app.services.action_report.snapshot_backed.request import ReportGenerationRequest


@pytest.mark.asyncio
async def test_generator_uses_legacy_path_by_default(db_session):
    # Ensure the ensure_service returns a simulated bundle response
    ensure_mock = AsyncMock()
    from types import SimpleNamespace

    ensure_mock.ensure.return_value = SimpleNamespace(
        bundle_uuid=None,  # Will raise early, proving it hit the main generate path before stage runner
        status="complete",
        coverage_summary={},
        freshness_summary={"overall": "fresh"},
        missing_sources=[],
        warnings=[],
        created=True,
    )

    generator = SnapshotBackedReportGenerator(
        db_session,
        ensure_service=ensure_mock,
    )

    request = ReportGenerationRequest(
        market="kr",
        account_scope="kis_live",
        created_by_profile="PROFILER",
        title="Title",
        summary="Summary",
        kst_date="2026-05-20",
        auto_compose=False,  # default
    )

    from app.services.action_report.snapshot_backed.generator import (
        SnapshotBackedReportGeneratorError,
    )

    with pytest.raises(SnapshotBackedReportGeneratorError) as exc:
        await generator.generate(request)

    assert "bundle ensure returned no bundle_uuid" in str(exc.value)
    ensure_mock.ensure.assert_called_once()


from tests.services.action_report.snapshot_backed.test_generator import (
    _FakeEnsureService,
    _FakeIngestionService,
    _FakeSnapshotsRepository,
    _ensure_response,
    _make_request,
)


@pytest.mark.asyncio
async def test_intraday_report_never_empty_items() -> None:
    # market=kr/account=kis_live, user_id=None (portfolio unavailable),
    # 빈 후보 -> 생성 결과 items_count >= 1, floor item은 data_gap.
    ensure = _FakeEnsureService(
        _ensure_response(
            status="partial",
            freshness_summary={
                "overall": "unavailable",
                "portfolio": {
                    "status": "unavailable",
                    "reason_code": "user_id_missing",
                },
                "journal": {"status": "fresh"},
                "watch_context": {"status": "fresh"},
                "market": {"status": "fresh"},
            },
            missing_sources=["portfolio"],
        )
    )
    ingest = _FakeIngestionService()
    gen = SnapshotBackedReportGenerator(
        session=object(),
        ensure_service=ensure,
        ingestion_service=ingest,
        snapshots_repository=_FakeSnapshotsRepository(),
    )
    # policy_version defaults to "intraday_action_report_v1" in _make_request
    response = await gen.generate(_make_request(status="draft"))
    assert response.items_count >= 1

    # Verify that the floor item has decision_bucket == "deferred_no_action" and action_verdict == "data_gap"
    assert len(ingest.calls) == 1
    sent = ingest.calls[0]
    assert len(sent.items) == 1
    item = sent.items[0]
    assert item.decision_bucket == "deferred_no_action"
    assert item.evidence_snapshot["action_verdict"] == "data_gap"

