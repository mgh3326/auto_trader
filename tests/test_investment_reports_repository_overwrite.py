"""ROB-352 Slice A — repository overwrite primitives (delete items + update report)."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.repository import InvestmentReportsRepository


@pytest.mark.asyncio
async def test_delete_items_for_report_removes_only_that_reports_items(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rep:a",
        report_type="t",
        market="kr",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title="t",
        summary="s",
        status="draft",
        report_metadata={},
    )
    await repo.insert_item(
        report_id=report.id,
        idempotency_key="it:a",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        target_kind="asset",
        priority=0,
        rationale="r",
        evidence_snapshot={},
    )
    assert len(await repo.list_items_for_report(report.id)) == 1

    await repo.delete_items_for_report(report.id)
    assert await repo.list_items_for_report(report.id) == []


@pytest.mark.asyncio
async def test_update_report_changes_scalar_and_jsonb_fields(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await repo.insert_report(
        idempotency_key="rep:b",
        report_type="t",
        market="kr",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title="old",
        summary="old",
        status="draft",
        report_metadata={"k": 1},
    )
    await repo.update_report(
        report.id,
        title="new",
        summary="new-summary",
        report_metadata={"k": 2, "overwrite_reason": "redo"},
    )
    refreshed = await repo.get_report_by_id(report.id)
    assert refreshed is not None
    assert refreshed.title == "new"
    assert refreshed.summary == "new-summary"
    assert refreshed.report_metadata == {"k": 2, "overwrite_reason": "redo"}
    # report_uuid stays stable across an in-place update.
    assert refreshed.report_uuid == report.report_uuid
