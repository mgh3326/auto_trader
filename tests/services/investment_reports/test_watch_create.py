from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
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
    assert alert.alert_metadata["source_link"] == "direct_without_report"
    assert alert.alert_metadata["ticket_hint"] == "support-reclaim"
    assert alert.source_report_uuid is None
    assert alert.source_item_uuid is None

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


# ---------------------------------------------------------------------------
# task99 — top-level action_mode surface + silent-downgrade removal
# ---------------------------------------------------------------------------


def _crypto_watch_payload(**overrides) -> dict:
    """Anonymized crypto watch payload shaped like a real operator request."""
    payload = {
        "created_by": "operator-session",
        "market": "crypto",
        "symbol": "KRW-BTC",
        "intent": "buy_review",
        "rationale": "weekly support retest watch",
        "watch_condition": {
            "metric": "price",
            "operator": "below",
            "threshold": 150000000,
        },
        "valid_until": (now_kst() + timedelta(days=7)).isoformat(),
        "trigger_checklist": ["confirm daily close"],
        "max_action": {"side": "buy", "krw_cap": 300000},
        "metadata": {"source": "weekly-review"},
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("top", "nested", "expected"),
    [
        (None, None, "notify_only"),
        ("notify_only", None, "notify_only"),
        ("approval_required", None, "approval_required"),
        (None, "notify_only", "notify_only"),
        (None, "approval_required", "approval_required"),
        (None, "preview_only", "preview_only"),
        ("notify_only", "notify_only", "notify_only"),
        ("approval_required", "approval_required", "approval_required"),
    ],
)
def test_watch_create_request_resolves_action_mode(
    top: str | None, nested: str | None, expected: str
) -> None:
    condition: dict = {"metric": "price", "operator": "below", "threshold": 150000000}
    if nested is not None:
        condition["action_mode"] = nested
    request = CreateInvestmentWatchRequest.model_validate(
        _crypto_watch_payload(watch_condition=condition, action_mode=top)
    )

    assert request.watch_condition.action_mode == expected


@pytest.mark.parametrize(
    ("top", "nested"),
    [
        ("notify_only", "approval_required"),
        ("approval_required", "notify_only"),
    ],
)
def test_watch_create_request_rejects_action_mode_conflict(
    top: str, nested: str
) -> None:
    condition = {
        "metric": "price",
        "operator": "below",
        "threshold": 150000000,
        "action_mode": nested,
    }
    with pytest.raises(ValidationError) as exc_info:
        CreateInvestmentWatchRequest.model_validate(
            _crypto_watch_payload(watch_condition=condition, action_mode=top)
        )

    assert "action_mode_conflict" in str(exc_info.value)


@pytest.mark.parametrize("bad_mode", ["auto_execute_mock", "preview_only"])
def test_watch_create_request_top_level_action_mode_rejects_execution_modes(
    bad_mode: str,
) -> None:
    with pytest.raises(ValidationError):
        CreateInvestmentWatchRequest.model_validate(
            _crypto_watch_payload(action_mode=bad_mode)
        )


def test_watch_create_request_approval_required_needs_max_action() -> None:
    with pytest.raises(ValidationError) as exc_info:
        CreateInvestmentWatchRequest.model_validate(
            _crypto_watch_payload(action_mode="approval_required", max_action={})
        )

    assert "max_action_required" in str(exc_info.value)


def test_watch_create_request_nested_approval_required_needs_max_action() -> None:
    condition = {
        "metric": "price",
        "operator": "below",
        "threshold": 150000000,
        "action_mode": "approval_required",
    }
    with pytest.raises(ValidationError) as exc_info:
        CreateInvestmentWatchRequest.model_validate(
            _crypto_watch_payload(watch_condition=condition, max_action={})
        )

    assert "max_action_required" in str(exc_info.value)


@pytest.mark.asyncio
async def test_direct_watch_create_persists_top_level_action_mode(
    session: AsyncSession,
) -> None:
    request = _request(
        watch_condition=WatchConditionPayload(
            metric="price", operator="above", threshold=70000
        ),
        action_mode="approval_required",
    )
    alert, idempotent = await DirectWatchCreateService(session).create(request)

    assert idempotent is False
    assert alert.action_mode == "approval_required"
    assert alert.max_action == {"side": "buy", "cash_fraction": 0.1}


@pytest.mark.asyncio
async def test_direct_watch_create_defaults_to_notify_only(
    session: AsyncSession,
) -> None:
    request = _request(
        watch_condition=WatchConditionPayload(
            metric="price", operator="above", threshold=70000
        ),
    )
    alert, _ = await DirectWatchCreateService(session).create(request)

    assert alert.action_mode == "notify_only"
