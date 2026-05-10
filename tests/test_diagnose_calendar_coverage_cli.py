"""Smoke test for the read-only diagnose_calendar_coverage CLI (ROB-167)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from app.schemas.calendar_freshness import (
    CalendarCoverage,
    CalendarSourceStatus,
    CoverageMatrixResponse,
)
from scripts import diagnose_calendar_coverage as cli


@pytest.mark.unit
@pytest.mark.asyncio
async def test_cli_prints_json_summary(monkeypatch, capsys) -> None:
    fake_matrix = CoverageMatrixResponse(
        fromDate=date(2026, 5, 11),
        toDate=date(2026, 5, 11),
        asOf=datetime.now(UTC),
        sources=[
            CalendarSourceStatus(
                source="finnhub",
                category="earnings",
                market="us",
                state="fresh",
                lastSuccessAt=datetime.now(UTC) - timedelta(hours=1),
                succeededPartitions=1,
                failedPartitions=0,
                missingPartitions=0,
                eventCount=12,
            ),
        ],
        partitions=[],
        coverage=CalendarCoverage(
            fromDate=date(2026, 5, 11),
            toDate=date(2026, 5, 11),
            expectedPartitions=3,
            succeededPartitions=1,
            failedPartitions=0,
            missingPartitions=2,
            totalEvents=12,
        ),
    )
    fake_svc = AsyncMock()
    fake_svc.get_coverage_matrix = AsyncMock(return_value=fake_matrix)
    monkeypatch.setattr(cli, "MarketEventsFreshnessService", lambda db: fake_svc)

    rc = await cli.run(
        from_date=date(2026, 5, 11), to_date=date(2026, 5, 11), as_json=True
    )

    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert rc == 0
    assert payload["coverage"]["expectedPartitions"] == 3
    assert payload["coverage"]["missingPartitions"] == 2
