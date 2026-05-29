"""ROB-352 Slice B — prior_reports excludes draft (smoke) reports."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


async def _make_report(repo, *, key, status, title):
    return await repo.insert_report(
        idempotency_key=key,
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile="t",
        title=title,
        summary="s",
        status=status,
        report_metadata={},
    )


@pytest.mark.asyncio
async def test_prior_reports_excludes_drafts(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    await _make_report(repo, key="draft:1", status="draft", title="hermes-smoke-1")
    await _make_report(repo, key="draft:2", status="draft", title="hermes-smoke-2")
    await _make_report(repo, key="pub:2", status="published", title="real-2")

    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us", account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1", n_prior=3,
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert titles == {"real-1", "real-2"}
    assert all(r.status != "draft" for r in ctx["prior_reports"])
