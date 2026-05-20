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
