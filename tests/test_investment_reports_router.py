"""ROB-265 Plan 3 — HTTP router tests.

We call the route functions directly with manually-supplied dependencies
rather than going through TestClient. The route bodies are the part
under test (serialisation + service wiring); the FastAPI plumbing is
covered by FastAPI itself.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.investment_reports import (
    get_investment_report,
    get_previous_report_context,
    list_investment_reports,
)
from app.schemas.investment_reports import (
    IngestReportItem,
    IngestReportRequest,
    RecordDecisionRequest,
    WatchConditionPayload,
)
from app.services.investment_reports.decisions import (
    InvestmentReportDecisionService,
)
from app.services.investment_reports.ingestion import (
    InvestmentReportIngestionService,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from tests._investment_reports_helpers import future_datetime, publish_report

_USER = SimpleNamespace(username="operator-test", id=1)


def _request(*, kst_date: str, **overrides) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": f"t-{kst_date}",
        "summary": "s",
        "kst_date": kst_date,
    }
    payload.update(overrides)
    return IngestReportRequest(**payload)


def _action_item(client_item_key: str = "action-1") -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        rationale="r",
    )


def _watch_item(client_item_key: str = "watch-1") -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind="watch",
        symbol="000660",
        intent="trend_recovery_review",
        rationale="r",
        watch_condition=WatchConditionPayload(
            metric="rsi", operator="below", threshold=30
        ),
        valid_until=future_datetime(),
    )


@pytest.mark.asyncio
async def test_list_returns_empty_when_no_reports(session: AsyncSession) -> None:
    service = InvestmentReportQueryService(session)
    response = await list_investment_reports(
        _user=_USER,
        service=service,
        market="kr",
        market_session=None,
        account_scope=None,
        status_filter=None,
        report_type=None,
        limit=20,
    )
    assert response.reports == []


@pytest.mark.asyncio
async def test_list_filters_by_market(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    await ingest.ingest(_request(kst_date="2026-05-18", market="kr"))
    await ingest.ingest(_request(kst_date="2026-05-18", market="us"))
    await session.commit()

    service = InvestmentReportQueryService(session)
    kr_response = await list_investment_reports(
        _user=_USER,
        service=service,
        market="kr",
        market_session=None,
        account_scope=None,
        status_filter=None,
        report_type=None,
        limit=20,
    )
    assert len(kr_response.reports) == 1
    assert kr_response.reports[0].market == "kr"


@pytest.mark.asyncio
async def test_get_bundle_returns_nested_response(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        _request(kst_date="2026-05-18", items=[_action_item(), _watch_item()])
    )

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    action_item = next(it for it in items if it.item_kind == "action")
    decisions_svc = InvestmentReportDecisionService(session)
    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=action_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    await session.commit()

    service = InvestmentReportQueryService(session)
    bundle = await get_investment_report(
        report_uuid=report.report_uuid, _user=_USER, service=service
    )
    assert bundle.report.report_uuid == report.report_uuid
    assert len(bundle.items) == 2
    # decision keyed by item_uuid string in the response shape
    assert str(action_item.item_uuid) in bundle.decisions_by_item_uuid
    assert len(bundle.decisions_by_item_uuid[str(action_item.item_uuid)]) == 1


@pytest.mark.asyncio
async def test_get_bundle_404_for_missing_report(session: AsyncSession) -> None:
    service = InvestmentReportQueryService(session)
    with pytest.raises(HTTPException) as exc_info:
        await get_investment_report(
            report_uuid=uuid.uuid4(), _user=_USER, service=service
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "not_found"


@pytest.mark.asyncio
async def test_previous_context_empty_for_unknown_market(
    session: AsyncSession,
) -> None:
    service = InvestmentReportQueryService(session)
    response = await get_previous_report_context(
        _user=_USER,
        service=service,
        market="kr",
        market_session=None,
        account_scope=None,
        report_type=None,
        exclude_report_uuid=None,
        n_prior=3,
    )
    assert response.prior_reports == []
    assert response.unresolved_deferred_items == []
    assert response.active_watches == []
    assert response.triggered_events == []
    assert response.recent_decisions == []


@pytest.mark.asyncio
async def test_previous_context_with_prior_reports(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    r1 = await ingest.ingest(_request(kst_date="2026-05-16"))
    r2 = await ingest.ingest(_request(kst_date="2026-05-17"))
    r3 = await ingest.ingest(_request(kst_date="2026-05-18"))
    # ROB-352 Slice B — prior context excludes drafts; publish so r1/r2 appear.
    await publish_report(session, r1)
    await publish_report(session, r2)
    await session.commit()

    service = InvestmentReportQueryService(session)
    response = await get_previous_report_context(
        _user=_USER,
        service=service,
        market="kr",
        market_session=None,
        account_scope=None,
        report_type=None,
        exclude_report_uuid=r3.report_uuid,
        n_prior=5,
    )
    prior_uuids = {r.report_uuid for r in response.prior_reports}
    assert prior_uuids == {r1.report_uuid, r2.report_uuid}


@pytest.mark.asyncio
async def test_get_bundle_groups_items(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    req = _request(kst_date="2026-05-18")
    # Add custom items
    item1 = _action_item(client_item_key="item-1")
    item1.decision_bucket = "new_buy_candidate"
    item2 = _action_item(client_item_key="item-2")
    item2.decision_bucket = "open_action"
    # Clear out default items and set our custom items
    req.items = [item1, item2]

    report = await ingest.ingest(req)
    await session.commit()

    service = InvestmentReportQueryService(session)
    bundle = await get_investment_report(
        report_uuid=report.report_uuid, _user=_USER, service=service
    )

    assert "new_buy_candidate" in bundle.item_groups
    assert "open_action" in bundle.item_groups
    assert len(bundle.item_groups["new_buy_candidate"]) == 1
    assert len(bundle.item_groups["open_action"]) == 1

    assert len(bundle.decision_rollup["new_candidate"]) == 1
    assert len(bundle.decision_rollup["held_action"]) == 1

    # ROB-322 — additive five-section review projection is wired end-to-end.
    review = bundle.review_sections
    assert review is not None
    by_key = {s.key: s for s in review.sections}
    assert [s.key for s in review.sections] == [
        "new_buy_candidate",
        "held_strategy_review",
        "watch_only",
        "excluded_or_unavailable",
    ]
    assert len(by_key["new_buy_candidate"].items) == 1
    assert len(by_key["held_strategy_review"].items) == 1  # open_action
    assert by_key["watch_only"].items == []
    assert by_key["excluded_or_unavailable"].items == []
    # No diagnostics + nothing excluded -> no summary.
    assert review.no_action_summary is None
