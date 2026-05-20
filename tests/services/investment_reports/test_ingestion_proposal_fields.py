"""ROB-274 — Round-trip tests for proposal-state fields through ingestion + repository.

The ingestion service must pass the new proposal-state fields
(operation/target_ref/current_state/proposed_state/diff/apply_policy)
through to the repository so they persist on the
``investment_report_items`` row. These tests assert the full
schema → service → DB round-trip.

Uses the shared ``session`` fixture from
``tests._investment_reports_helpers`` (pytest plugin in
``tests/conftest.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    TargetRefPayload,
    WatchConditionPayload,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)


@pytest.mark.asyncio
async def test_ingest_persists_proposal_fields(session: AsyncSession) -> None:
    """ROB-274 — operation/target_ref/current_state/apply_policy survive a round-trip."""

    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="cancel",
        intent="risk_review",
        rationale="r",
        target_ref=TargetRefPayload(
            type="investment_watch_alert", id="alert-1", status="active"
        ),
        current_state={"metric": "price", "operator": "above", "threshold": "100"},
        apply_policy="requires_user_approval",
    )
    request = IngestReportRequest(
        report_type="t",
        market="crypto",
        account_scope="upbit_live",
        created_by_profile="claude_code",
        title="t",
        summary="s",
        kst_date="2026-05-20",
        items=[item],
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.flush()

    row = (
        await session.execute(
            select(InvestmentReportItem).where(
                InvestmentReportItem.report_id == report.id
            )
        )
    ).scalar_one()
    assert row.operation == "cancel"
    # ``target_ref`` is serialised via ``model_dump(mode="json")`` which
    # includes the ``raw``/``candidates`` keys as None — assert the
    # caller-supplied subset to stay tolerant of Pydantic serialisation.
    assert row.target_ref["type"] == "investment_watch_alert"
    assert row.target_ref["id"] == "alert-1"
    assert row.target_ref["status"] == "active"
    assert row.current_state == {
        "metric": "price",
        "operator": "above",
        "threshold": "100",
    }
    assert row.apply_policy == "requires_user_approval"


@pytest.mark.asyncio
async def test_ingest_persists_modify_proposal_with_diff(
    session: AsyncSession,
) -> None:
    """ROB-274 — modify operation persists diff list + proposed_state as JSONB."""

    item = IngestReportItem(
        client_item_key="k1",
        item_kind="watch",
        operation="modify",
        intent="trend_recovery_review",
        rationale="r",
        target_ref=TargetRefPayload(
            type="investment_watch_alert", id="alert-1", status="active"
        ),
        current_state={"threshold": "100"},
        proposed_state={"threshold": "120"},
        diff=[{"field": "threshold", "from": "100", "to": "120"}],
        watch_condition=WatchConditionPayload(
            metric="price", operator="above", threshold=Decimal("120")
        ),
        valid_until=datetime.now(tz=UTC) + timedelta(days=7),
        apply_policy="requires_user_approval",
    )
    request = IngestReportRequest(
        report_type="t",
        market="crypto",
        account_scope="upbit_live",
        created_by_profile="claude_code",
        title="t",
        summary="s",
        kst_date="2026-05-20",
        items=[item],
    )
    svc = InvestmentReportIngestionService(session)
    report = await svc.ingest(request)
    await session.flush()

    row = (
        await session.execute(
            select(InvestmentReportItem).where(
                InvestmentReportItem.report_id == report.id
            )
        )
    ).scalar_one()
    assert row.operation == "modify"
    assert row.diff == [{"field": "threshold", "from": "100", "to": "120"}]
    assert row.proposed_state == {"threshold": "120"}
    assert row.current_state == {"threshold": "100"}
