from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.schemas.investment_reports import (
    CreateInvestmentWatchRequest,
    WatchConditionPayload,
)
from app.services.investment_reports.watch_create import DirectWatchCreateService


def _request(**overrides) -> CreateInvestmentWatchRequest:
    payload = {
        "created_by": "tradingcodex",
        "market": "kr",
        "symbol": "005930",
        "intent": "trend_recovery_review",
        "rationale": "price reclaimed support",
        "watch_condition": WatchConditionPayload(
            metric="price",
            operator="above",
            threshold=70000,
            action_mode="approval_required",
        ),
        "valid_until": now_kst() + timedelta(days=2),
        "trigger_checklist": ["confirm fresh L1", "open approved ticket"],
        "max_action": {"side": "buy", "cash_fraction": 0.1},
        "metadata": {"ticket_hint": "support-reclaim"},
    }
    payload.update(overrides)
    return CreateInvestmentWatchRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_direct_watch_create_persists_alert_without_report_rows(
    session: AsyncSession,
) -> None:
    alert, idempotent = await DirectWatchCreateService(session).create(_request())

    assert idempotent is False
    assert alert.status == "active"
    assert alert.market == "kr"
    assert alert.symbol == "005930"
    assert alert.metric == "price"
    assert alert.operator == "above"
    assert str(alert.threshold) in {"70000", "70000.00000000"}
    assert alert.threshold_key == "70000"
    assert alert.intent == "trend_recovery_review"
    assert alert.action_mode == "approval_required"
    assert alert.trigger_checklist == ["confirm fresh L1", "open approved ticket"]
    assert alert.max_action == {"side": "buy", "cash_fraction": 0.1}
    assert alert.alert_metadata["created_by"] == "tradingcodex"
    assert alert.alert_metadata["source_tool"] == "investment_watch_create"
    assert alert.alert_metadata["ticket_hint"] == "support-reclaim"

    report_count = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReport)
    )
    item_count = await session.scalar(
        sa.select(sa.func.count()).select_from(InvestmentReportItem)
    )
    assert report_count == 0
    assert item_count == 0


@pytest.mark.asyncio
async def test_direct_watch_create_is_idempotent_for_same_key(
    session: AsyncSession,
) -> None:
    service = DirectWatchCreateService(session)
    request = _request(idempotency_key="tcx:1")
    first, first_idempotent = await service.create(request)
    second, second_idempotent = await service.create(request)

    assert first_idempotent is False
    assert second_idempotent is True
    assert second.id == first.id
    assert second.alert_uuid == first.alert_uuid


@pytest.mark.asyncio
async def test_direct_watch_create_rejects_idempotency_collision(
    session: AsyncSession,
) -> None:
    service = DirectWatchCreateService(session)
    await service.create(_request(idempotency_key="tcx:collision"))

    with pytest.raises(ValueError, match="idempotency_key .* already used"):
        await service.create(_request(idempotency_key="tcx:collision", symbol="000660"))


@pytest.mark.asyncio
async def test_direct_watch_create_rejects_idempotency_collision_for_different_condition(
    session: AsyncSession,
) -> None:
    service = DirectWatchCreateService(session)
    await service.create(_request(idempotency_key="tcx:condition-collision"))

    with pytest.raises(ValueError, match="idempotency_key .* already used"):
        await service.create(
            _request(
                idempotency_key="tcx:condition-collision",
                watch_condition=WatchConditionPayload(
                    metric="price",
                    operator="above",
                    threshold=71000,
                    action_mode="approval_required",
                ),
            )
        )


@pytest.mark.asyncio
async def test_direct_watch_create_rejects_expired_valid_until(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="valid_until must be in the future"):
        await DirectWatchCreateService(session).create(
            _request(valid_until=now_kst() - timedelta(minutes=1))
        )


@pytest.mark.asyncio
async def test_direct_watch_create_rejects_auto_execute_mock(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValueError, match="auto_execute_mock"):
        await DirectWatchCreateService(session).create(
            _request(
                watch_condition=WatchConditionPayload(
                    metric="price",
                    operator="above",
                    threshold=70000,
                    action_mode="auto_execute_mock",
                )
            )
        )
