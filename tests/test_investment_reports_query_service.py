"""ROB-265 Plan 2 — Query service tests (list / latest / bundle / context)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import (
    ActivateWatchRequest,
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
from app.services.investment_reports.watch_activation import WatchActivationService
from tests._investment_reports_helpers import (
    future_datetime,
)
from tests._investment_reports_helpers import (
    publish_report as _publish,
)


def _request(*, kst_date: str, market: str = "kr", **overrides) -> IngestReportRequest:
    payload: dict = {
        "report_type": "kr_morning",
        "market": market,
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


def _action_item(
    client_item_key: str = "action-1", symbol: str = "005930"
) -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind="action",
        symbol=symbol,
        side="buy",
        intent="buy_review",
        rationale="r",
    )


def _watch_item(
    client_item_key: str = "watch-1", symbol: str = "000660"
) -> IngestReportItem:
    return IngestReportItem(
        client_item_key=client_item_key,
        item_kind="watch",
        symbol=symbol,
        intent="trend_recovery_review",
        rationale="r",
        watch_condition=WatchConditionPayload(
            metric="rsi", operator="below", threshold=30
        ),
        valid_until=future_datetime(),
    )


@pytest.mark.asyncio
async def test_list_reports_orders_newest_first(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    await ingest.ingest(_request(kst_date="2026-05-16"))
    await ingest.ingest(_request(kst_date="2026-05-17"))
    last = await ingest.ingest(_request(kst_date="2026-05-18"))

    query = InvestmentReportQueryService(session)
    reports = await query.list_reports(market="kr")
    assert len(reports) == 3
    assert reports[0].id == last.id


@pytest.mark.asyncio
async def test_list_reports_filters(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    await ingest.ingest(_request(kst_date="2026-05-18", market="kr"))
    await ingest.ingest(_request(kst_date="2026-05-18", market="us"))

    query = InvestmentReportQueryService(session)
    kr_only = await query.list_reports(market="kr")
    assert len(kr_only) == 1
    assert kr_only[0].market == "kr"


@pytest.mark.asyncio
async def test_latest_report_returns_none_when_empty(session: AsyncSession) -> None:
    query = InvestmentReportQueryService(session)
    assert await query.latest_report(market="kr") is None


@pytest.mark.asyncio
async def test_latest_report_returns_most_recent(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    await ingest.ingest(_request(kst_date="2026-05-16"))
    expected = await ingest.ingest(_request(kst_date="2026-05-17"))
    query = InvestmentReportQueryService(session)
    latest = await query.latest_report(market="kr")
    assert latest is not None
    assert latest.id == expected.id


@pytest.mark.asyncio
async def test_get_bundle_returns_nested_shapes(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        _request(kst_date="2026-05-18", items=[_action_item(), _watch_item()])
    )

    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = next(it for it in items if it.item_kind == "watch")
    action_item = next(it for it in items if it.item_kind == "action")

    decisions_svc = InvestmentReportDecisionService(session)
    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=action_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=watch_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=watch_item.item_uuid, actor="operator-test")
    )

    query = InvestmentReportQueryService(session)
    bundle = await query.get_bundle(report.report_uuid)
    assert bundle is not None
    assert bundle["report"].id == report.id
    assert len(bundle["items"]) == 2
    assert len(bundle["alerts"]) == 1
    assert len(bundle["decisions_by_item"][action_item.id]) == 1
    assert len(bundle["decisions_by_item"][watch_item.id]) == 1


@pytest.mark.asyncio
async def test_get_bundle_returns_none_for_missing_report(
    session: AsyncSession,
) -> None:
    query = InvestmentReportQueryService(session)
    assert await query.get_bundle(uuid.uuid4()) is None


@pytest.mark.asyncio
async def test_previous_context_empty_when_no_prior(session: AsyncSession) -> None:
    query = InvestmentReportQueryService(session)
    ctx = await query.previous_report_context(market="kr")
    assert ctx["prior_reports"] == []
    assert ctx["unresolved_deferred_items"] == []
    assert ctx["active_watches"] == []
    assert ctx["triggered_events"] == []
    assert ctx["recent_decisions"] == []


@pytest.mark.asyncio
async def test_previous_context_aggregates_across_prior_reports(
    session: AsyncSession,
) -> None:
    ingest = InvestmentReportIngestionService(session)
    decisions_svc = InvestmentReportDecisionService(session)
    activation_svc = WatchActivationService(session)

    r1 = await ingest.ingest(
        _request(
            kst_date="2026-05-16",
            items=[
                _action_item(client_item_key="r1-action-1"),
                _watch_item(client_item_key="r1-watch-1"),
            ],
        )
    )
    r2 = await ingest.ingest(
        _request(
            kst_date="2026-05-17",
            items=[_action_item(client_item_key="r2-action-1", symbol="035420")],
        )
    )
    # ROB-352: publish r1 and r2 so they appear in prior context (drafts excluded).
    await _publish(session, r1)
    await _publish(session, r2)

    repo = InvestmentReportsRepository(session)
    r1_items = await repo.list_items_for_report(r1.id)
    r2_items = await repo.list_items_for_report(r2.id)
    action_r1 = next(it for it in r1_items if it.item_kind == "action")
    watch_r1 = next(it for it in r1_items if it.item_kind == "watch")
    action_r2 = r2_items[0]

    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=action_r1.item_uuid, decision="defer", actor="op"
        )
    )
    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=watch_r1.item_uuid, decision="approve", actor="op"
        )
    )
    await activation_svc.activate(
        ActivateWatchRequest(item_uuid=watch_r1.item_uuid, actor="op")
    )
    await decisions_svc.record(
        RecordDecisionRequest(
            item_uuid=action_r2.item_uuid, decision="approve", actor="op"
        )
    )

    r3 = await ingest.ingest(_request(kst_date="2026-05-18"))

    query = InvestmentReportQueryService(session)
    ctx = await query.previous_report_context(
        market="kr", exclude_report_uuid=r3.report_uuid, n_prior=5
    )

    prior_ids = {r.id for r in ctx["prior_reports"]}
    assert prior_ids == {r1.id, r2.id}

    deferred_ids = {it.id for it in ctx["unresolved_deferred_items"]}
    assert deferred_ids == {action_r1.id}

    assert len(ctx["active_watches"]) == 1
    assert ctx["active_watches"][0].source_item_uuid == watch_r1.item_uuid

    assert {d.decision for d in ctx["recent_decisions"]} >= {"defer", "approve"}


@pytest.mark.asyncio
async def test_previous_context_excludes_named_report(
    session: AsyncSession,
) -> None:
    ingest = InvestmentReportIngestionService(session)
    r1 = await ingest.ingest(_request(kst_date="2026-05-17"))
    r2 = await ingest.ingest(_request(kst_date="2026-05-18"))
    # ROB-352: publish r1 so it appears in prior context (drafts excluded).
    await _publish(session, r1)
    await _publish(session, r2)

    query = InvestmentReportQueryService(session)
    ctx = await query.previous_report_context(
        market="kr", exclude_report_uuid=r2.report_uuid, n_prior=5
    )
    assert {r.id for r in ctx["prior_reports"]} == {r1.id}


@pytest.mark.asyncio
async def test_previous_context_respects_n_prior_limit(
    session: AsyncSession,
) -> None:
    ingest = InvestmentReportIngestionService(session)
    reports = []
    for date in ("2026-05-14", "2026-05-15", "2026-05-16", "2026-05-17"):
        r = await ingest.ingest(_request(kst_date=date))
        reports.append(r)
    # ROB-352: publish all so they appear in prior context (drafts excluded).
    for r in reports:
        await _publish(session, r)

    query = InvestmentReportQueryService(session)
    ctx = await query.previous_report_context(market="kr", n_prior=2)
    assert len(ctx["prior_reports"]) == 2
