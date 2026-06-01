"""ROB-265 Plan 2 — Watch activation service tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReportItem, InvestmentWatchAlert
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
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_reports.watch_activation import WatchActivationService
from tests._investment_reports_helpers import future_datetime


async def _seed_approved_watch_item(
    session: AsyncSession,
    *,
    kst_date: str = "2026-05-18",
    client_item_key: str = "watch-1",
    symbol: str = "005930",
) -> InvestmentReportItem:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            market_session="regular",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="test",
            title="t",
            summary="s",
            kst_date=kst_date,
            items=[
                IngestReportItem(
                    client_item_key=client_item_key,
                    item_kind="watch",
                    symbol=symbol,
                    intent="trend_recovery_review",
                    rationale="r",
                    watch_condition=WatchConditionPayload(
                        metric="rsi", operator="below", threshold=Decimal("30")
                    ),
                    valid_until=future_datetime(),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = items[0]

    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=watch_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    refreshed = await repo.get_item_by_uuid(watch_item.item_uuid)
    assert refreshed is not None
    assert refreshed.status == "approved"
    return refreshed


@pytest.mark.asyncio
async def test_activate_copies_snapshot_and_transitions_item(
    session: AsyncSession,
) -> None:
    item = await _seed_approved_watch_item(session)
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    assert alert.market == "kr"
    assert alert.target_kind == "asset"
    assert alert.symbol == "005930"
    assert alert.metric == "rsi"
    assert alert.operator == "below"
    assert Decimal(alert.threshold) == Decimal("30")
    assert alert.threshold_key == "30"
    assert alert.intent == "trend_recovery_review"
    assert alert.action_mode == "notify_only"
    assert alert.status == "active"
    assert alert.source_item_uuid == item.item_uuid

    repo = InvestmentReportsRepository(session)
    refreshed = await repo.get_item_by_uuid(item.item_uuid)
    assert refreshed.status == "activated"


@pytest.mark.asyncio
async def test_activate_is_idempotent_per_source_item(
    session: AsyncSession,
) -> None:
    item = await _seed_approved_watch_item(session)
    service = WatchActivationService(session)
    first = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    second = await service.activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    assert first.alert_uuid == second.alert_uuid
    assert first.id == second.id


@pytest.mark.asyncio
async def test_activate_rejects_non_watch_item(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="t",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    client_item_key="action-1",
                    item_kind="action",
                    symbol="005930",
                    side="buy",
                    intent="buy_review",
                    rationale="r",
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    action_item = items[0]
    await InvestmentReportDecisionService(session).record(
        RecordDecisionRequest(
            item_uuid=action_item.item_uuid,
            decision="approve",
            actor="operator-test",
        )
    )
    with pytest.raises(ValueError, match="only watch items"):
        await WatchActivationService(session).activate(
            ActivateWatchRequest(item_uuid=action_item.item_uuid, actor="operator-test")
        )


@pytest.mark.asyncio
async def test_activate_rejects_unapproved_watch(session: AsyncSession) -> None:
    ingest = InvestmentReportIngestionService(session)
    report = await ingest.ingest(
        IngestReportRequest(
            report_type="kr_morning",
            market="kr",
            account_scope="kis_mock",
            execution_mode="mock_preview",
            created_by_profile="t",
            title="t",
            summary="s",
            kst_date="2026-05-18",
            items=[
                IngestReportItem(
                    client_item_key="watch-1",
                    item_kind="watch",
                    symbol="005930",
                    intent="trend_recovery_review",
                    rationale="r",
                    watch_condition=WatchConditionPayload(
                        metric="rsi", operator="below", threshold=30
                    ),
                    valid_until=future_datetime(),
                )
            ],
        )
    )
    repo = InvestmentReportsRepository(session)
    items = await repo.list_items_for_report(report.id)
    watch_item = items[0]
    with pytest.raises(ValueError, match="only approved items"):
        await WatchActivationService(session).activate(
            ActivateWatchRequest(item_uuid=watch_item.item_uuid, actor="operator-test")
        )


@pytest.mark.asyncio
async def test_activate_rejects_unknown_item(session: AsyncSession) -> None:
    with pytest.raises(ValueError, match="item not found"):
        await WatchActivationService(session).activate(
            ActivateWatchRequest(item_uuid=uuid.uuid4(), actor="operator-test")
        )


@pytest.mark.asyncio
async def test_alert_snapshot_does_not_mutate_when_item_changes(
    session: AsyncSession,
) -> None:
    """Once activated, mutating the source item does not change the alert."""
    item = await _seed_approved_watch_item(session)
    alert = await WatchActivationService(session).activate(
        ActivateWatchRequest(item_uuid=item.item_uuid, actor="operator-test")
    )
    original_rationale = alert.rationale
    alert_key = alert.idempotency_key

    repo = InvestmentReportsRepository(session)
    await session.execute(
        sa.update(InvestmentReportItem)
        .where(InvestmentReportItem.id == item.id)
        .values(rationale="something else entirely")
    )
    await session.commit()

    refreshed_alert = await repo.get_alert_by_idempotency_key(alert_key)
    assert refreshed_alert is not None
    assert refreshed_alert.rationale == original_rationale


@pytest.mark.asyncio
async def test_caller_supplied_idempotency_key_cross_item_collision_rejected(
    session: AsyncSession,
) -> None:
    """Plan 2 hardening #1: a caller-supplied idempotency_key that collides
    with an existing alert sourced from a DIFFERENT item must raise, not
    silently return the wrong alert.
    """
    item_a = await _seed_approved_watch_item(
        session, client_item_key="watch-a", symbol="005930"
    )
    item_b = await _seed_approved_watch_item(
        session, kst_date="2026-05-19", client_item_key="watch-b", symbol="000660"
    )
    shared_key = f"shared-alert-{uuid.uuid4()}"
    service = WatchActivationService(session)

    await service.activate(
        ActivateWatchRequest(
            item_uuid=item_a.item_uuid,
            actor="operator",
            idempotency_key=shared_key,
        )
    )
    with pytest.raises(ValueError, match="already used for a different watch item"):
        await service.activate(
            ActivateWatchRequest(
                item_uuid=item_b.item_uuid,
                actor="operator",
                idempotency_key=shared_key,
            )
        )


@pytest.mark.asyncio
async def test_alert_accepts_between_operator_and_conditions(
    session: AsyncSession,
) -> None:
    alert = InvestmentWatchAlert(
        alert_uuid=uuid.uuid4(),
        idempotency_key=f"k-{uuid.uuid4()}",
        source_report_uuid=uuid.uuid4(),
        source_item_uuid=uuid.uuid4(),
        market="kr",
        target_kind="asset",
        symbol="005930",
        metric="price",
        operator="between",
        threshold=Decimal("50000"),
        threshold_high=Decimal("55000"),
        threshold_key="and(price:between:50000-55000)",
        conditions=[{"metric": "price", "op": "between", "low": "50000", "high": "55000"}],
        combine="and",
        intent="buy_review",
        action_mode="notify_only",
        rationale="zone buy",
        valid_until=future_datetime(),
    )
    session.add(alert)
    await session.commit()
    fetched = await session.get(InvestmentWatchAlert, alert.id)
    assert fetched.operator == "between"
    assert fetched.conditions[0]["op"] == "between"
    assert fetched.combine == "and"

