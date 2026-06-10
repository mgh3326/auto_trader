"""ROB-265 Plan 2 — Repository DAO tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from tests._investment_reports_helpers import future_datetime


async def _insert_report(
    repo: InvestmentReportsRepository, **overrides
) -> InvestmentReport:
    fields: dict = {
        "report_uuid": uuid.uuid4(),
        "idempotency_key": f"rk-{uuid.uuid4()}",
        "report_type": "kr_morning",
        "market": "kr",
        "market_session": "regular",
        "account_scope": "kis_mock",
        "execution_mode": "mock_preview",
        "created_by_profile": "test",
        "title": "t",
        "summary": "s",
        "status": "draft",
    }
    fields.update(overrides)
    return await repo.insert_report(**fields)


async def _insert_item(
    repo: InvestmentReportsRepository, report_id: int, **overrides
) -> InvestmentReportItem:
    fields: dict = {
        "report_id": report_id,
        "item_uuid": uuid.uuid4(),
        "idempotency_key": f"ik-{uuid.uuid4()}",
        "item_kind": "action",
        "symbol": "005930",
        "side": "buy",
        "intent": "buy_review",
        "target_kind": "asset",
        "priority": 10,
        "rationale": "r",
    }
    fields.update(overrides)
    return await repo.insert_item(**fields)


@pytest.mark.asyncio
async def test_report_insert_and_get_by_uuid(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    row = await _insert_report(repo)
    fetched = await repo.get_report_by_uuid(row.report_uuid)
    assert fetched is not None
    assert fetched.id == row.id


@pytest.mark.asyncio
async def test_report_get_by_idempotency_key(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    key = f"ik-{uuid.uuid4()}"
    row = await _insert_report(repo, idempotency_key=key)
    fetched = await repo.get_report_by_idempotency_key(key)
    assert fetched is not None
    assert fetched.id == row.id


@pytest.mark.asyncio
async def test_list_reports_filters_by_market_and_status(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    await _insert_report(repo, market="kr", status="draft")
    await _insert_report(repo, market="us", status="draft")
    await _insert_report(repo, market="kr", status="published")

    kr_drafts = await repo.list_reports(market="kr", status="draft")
    assert len(kr_drafts) == 1
    assert kr_drafts[0].market == "kr"
    assert kr_drafts[0].status == "draft"


@pytest.mark.asyncio
async def test_latest_report_returns_most_recent(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    await _insert_report(repo, market="kr")
    await _insert_report(repo, market="kr")
    third = await _insert_report(repo, market="kr")
    latest = await repo.latest_report(market="kr")
    assert latest is not None
    assert latest.id == third.id


@pytest.mark.asyncio
async def test_item_insert_and_list_for_report(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    await _insert_item(repo, report.id)
    await _insert_item(repo, report.id, symbol="000660")
    items = await repo.list_items_for_report(report.id)
    assert len(items) == 2
    assert {it.symbol for it in items} == {"005930", "000660"}


@pytest.mark.asyncio
async def test_update_item_status(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id)
    await repo.update_item_status(item.id, "approved")
    await session.refresh(item)
    assert item.status == "approved"


@pytest.mark.asyncio
async def test_decision_insert_and_lookup(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id)
    key = f"dec-{uuid.uuid4()}"
    decision = await repo.insert_decision(
        item_id=item.id,
        decision_uuid=uuid.uuid4(),
        idempotency_key=key,
        decision="approve",
        actor="operator",
    )
    fetched = await repo.get_decision_by_idempotency_key(key)
    assert fetched is not None
    assert fetched.id == decision.id


@pytest.mark.asyncio
async def test_alert_insert_and_active_listing(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id)
    alert = await repo.insert_alert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"a-{uuid.uuid4()}",
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="r",
        valid_until=future_datetime(),
    )
    actives = await repo.list_active_alerts(market="kr")
    assert any(a.id == alert.id for a in actives)


@pytest.mark.asyncio
async def test_event_insert_and_list_for_alert(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id)
    alert = await repo.insert_alert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"a-{uuid.uuid4()}",
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="r",
        valid_until=future_datetime(),
    )
    event = await repo.insert_event(
        event_uuid=uuid.uuid4(),
        idempotency_key=f"e-{uuid.uuid4()}",
        alert_id=alert.id,
        source_report_uuid=report.report_uuid,
        source_item_uuid=item.item_uuid,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        current_value=69500,
        outcome="notified",
        correlation_id=str(uuid.uuid4()),
        kst_date="2026-05-18",
    )
    events = await repo.list_events_for_alert(alert.id)
    assert len(events) == 1
    assert events[0].id == event.id


@pytest.mark.asyncio
async def test_list_alerts_for_source_reports(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report1 = await _insert_report(repo)
    report2 = await _insert_report(repo)
    item1 = await _insert_item(repo, report1.id)
    item2 = await _insert_item(repo, report2.id)
    await repo.insert_alert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"a1-{uuid.uuid4()}",
        source_report_uuid=report1.report_uuid,
        source_item_uuid=item1.item_uuid,
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="below",
        threshold=70000,
        threshold_key="70000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="r",
        valid_until=future_datetime(),
    )
    await repo.insert_alert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"a2-{uuid.uuid4()}",
        source_report_uuid=report2.report_uuid,
        source_item_uuid=item2.item_uuid,
        market="kr",
        target_kind="asset",
        symbol="000660",
        metric="price",
        operator="below",
        threshold=60000,
        threshold_key="60000",
        intent="buy_review",
        action_mode="notify_only",
        rationale="r",
        valid_until=future_datetime(),
    )
    only_report1 = await repo.list_alerts_for_source_reports([report1.report_uuid])
    assert len(only_report1) == 1
    assert only_report1[0].symbol == "005930"


@pytest.mark.asyncio
async def test_item_get_by_idempotency_key(session: AsyncSession) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    item = await _insert_item(repo, report.id, idempotency_key="item:dedupe")

    fetched = await repo.get_item_by_idempotency_key("item:dedupe")

    assert fetched is not None
    assert fetched.id == item.id


@pytest.mark.asyncio
async def test_find_item_by_report_client_key_from_metadata(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    other = await _insert_report(repo)
    item = await _insert_item(
        repo,
        report.id,
        item_metadata={"client_item_key": "increment-1"},
    )
    await _insert_item(
        repo,
        other.id,
        item_metadata={"client_item_key": "increment-1"},
    )

    fetched = await repo.find_item_by_report_client_key(report.id, "increment-1")

    assert fetched is not None
    assert fetched.id == item.id


@pytest.mark.asyncio
async def test_find_item_by_report_client_key_ignores_missing_metadata(
    session: AsyncSession,
) -> None:
    repo = InvestmentReportsRepository(session)
    report = await _insert_report(repo)
    await _insert_item(repo, report.id, item_metadata={})

    fetched = await repo.find_item_by_report_client_key(report.id, "increment-1")

    assert fetched is None
