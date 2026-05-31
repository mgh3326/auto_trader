"""ROB-352 Slice B — prior_reports excludes draft (smoke) reports."""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


async def _make_report(repo, *, key, status, title, created_by_profile="t"):
    return await repo.insert_report(
        idempotency_key=key,
        report_type="snapshot_backed_advisory_v1",
        market="us",
        market_session=None,
        account_scope="kis_live",
        execution_mode="advisory_only",
        created_by_profile=created_by_profile,
        title=title,
        summary="s",
        status=status,
        report_metadata={},
    )


@pytest.mark.asyncio
async def test_prior_reports_excludes_drafts_by_default(session: AsyncSession) -> None:
    """Default draft_policy='exclude' drops ALL drafts — advisory and smoke alike."""
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    await _make_report(repo, key="draft:1", status="draft", title="hermes-smoke-1")
    # An advisory draft (HERMES_ADVISOR) is ALSO excluded under the default.
    await _make_report(
        repo,
        key="draft:adv",
        status="draft",
        title="advisory-1",
        created_by_profile="HERMES_ADVISOR",
    )
    await _make_report(repo, key="pub:2", status="published", title="real-2")

    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us",
        account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1",
        n_prior=4,
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert titles == {"real-1", "real-2"}
    assert all(r.status != "draft" for r in ctx["prior_reports"])


@pytest.mark.asyncio
async def test_prior_reports_advisory_only_includes_advisory_excludes_smoke(
    session: AsyncSession,
) -> None:
    """draft_policy='advisory_only' admits advisory drafts (HERMES_ADVISOR) as
    prior context but still drops smoke/test drafts (any other profile)."""
    repo = InvestmentReportsRepository(session)
    await _make_report(repo, key="pub:1", status="published", title="real-1")
    # smoke draft — created by a test/CI profile, must stay excluded.
    await _make_report(
        repo,
        key="draft:smoke",
        status="draft",
        title="hermes-smoke-1",
        created_by_profile="t",
    )
    # advisory draft — the genuine Hermes advisory baseline, must be admitted.
    await _make_report(
        repo,
        key="draft:adv",
        status="draft",
        title="advisory-1",
        created_by_profile="HERMES_ADVISOR",
    )
    await _make_report(repo, key="pub:2", status="published", title="real-2")

    svc = InvestmentReportQueryService(session)
    ctx = await svc.previous_report_context(
        market="us",
        account_scope="kis_live",
        report_type="snapshot_backed_advisory_v1",
        n_prior=4,
        draft_policy="advisory_only",
    )
    titles = {r.title for r in ctx["prior_reports"]}
    assert titles == {"real-1", "real-2", "advisory-1"}
    assert "hermes-smoke-1" not in titles


@pytest.mark.asyncio
async def test_prior_reports_rejects_unknown_draft_policy(
    session: AsyncSession,
) -> None:
    """There is no 'all' policy — an unknown value is a hard error at the
    service layer (the MCP handler fails closed to 'exclude' separately)."""
    svc = InvestmentReportQueryService(session)
    with pytest.raises(ValueError, match="draft_policy"):
        await svc.previous_report_context(
            market="us",
            account_scope="kis_live",
            report_type="snapshot_backed_advisory_v1",
            draft_policy="all",
        )
