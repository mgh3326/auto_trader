"""ROB-265 Plan 4 — Hermes notification client tests."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.services.hermes_client import (
    HermesNotificationClient,
    ReviewTriggerPayload,
    build_invest_links,
    build_operator_action_guidance,
    planned_action_from_max_action,
    price_guidance_from_watch_recommendation,
    trigger_checklist_from_raw,
)


def _base_payload(**overrides) -> ReviewTriggerPayload:
    payload: dict = {
        "event_uuid": uuid.uuid4(),
        "alert_uuid": uuid.uuid4(),
        "source_report_uuid": uuid.uuid4(),
        "source_item_uuid": uuid.uuid4(),
        "correlation_id": "corr-test-1",
        "kst_date": "2026-05-18",
        "market": "kr",
        "target_kind": "asset",
        "symbol": "005930",
        "metric": "rsi",
        "operator": "below",
        "threshold": Decimal("30"),
        "threshold_key": "30",
        "intent": "trend_recovery_review",
        "action_mode": "notify_only",
        "current_value": Decimal("28.5"),
        "scanner_snapshot": {"rsi_14": 28.5, "close": 68000},
        "outcome": "notified",
    }
    payload.update(overrides)
    return ReviewTriggerPayload(**payload)


def test_payload_rejects_extra_fields() -> None:
    """Locked design: the payload contract is closed (extra='forbid')."""
    with pytest.raises(ValidationError):
        ReviewTriggerPayload(
            event_uuid=uuid.uuid4(),
            alert_uuid=uuid.uuid4(),
            source_report_uuid=uuid.uuid4(),
            source_item_uuid=uuid.uuid4(),
            correlation_id="x",
            kst_date="2026-05-18",
            market="kr",
            target_kind="asset",
            symbol="005930",
            metric="rsi",
            operator="below",
            threshold=Decimal("30"),
            threshold_key="30",
            intent="trend_recovery_review",
            action_mode="notify_only",
            current_value=None,
            scanner_snapshot={},
            outcome="notified",
            # not in the schema — must be rejected
            stray_field="x",  # type: ignore[call-arg]
        )


@pytest.mark.asyncio
async def test_close_is_a_noop() -> None:
    # ROB-986: webhook HTTP client removed; close() stays a harmless no-op
    # so callers' symmetric close() calls keep working.
    await HermesNotificationClient().close()


# --- ROB-500 Tests ---

_REPORT_UUID = uuid.UUID("70019e8d-1ee6-493f-adeb-5d9301d5ea48")
_EVENT_UUID = uuid.UUID("f912d55f-d1b3-4971-a362-998bd9ffa6b4")
_ALERT_UUID = uuid.UUID("5e32ec11-f4ed-4ef7-9a84-561a5fb2be79")


def test_build_invest_links_full() -> None:
    links = build_invest_links(
        market="crypto",
        symbol="KRW-BTC",
        source_report_uuid=_REPORT_UUID,
        event_uuid=_EVENT_UUID,
        alert_uuid=_ALERT_UUID,
    )
    assert links.report_path == f"/invest/reports/{_REPORT_UUID}"
    assert links.stock_path == "/invest/stocks/crypto/KRW-BTC"
    assert links.event_anchor == (
        f"/invest/reports/{_REPORT_UUID}#watch-event-{_EVENT_UUID}"
    )
    assert links.alert_anchor == (
        f"/invest/reports/{_REPORT_UUID}#watch-alert-{_ALERT_UUID}"
    )


def test_build_invest_links_without_event_uuid_omits_event_anchor() -> None:
    links = build_invest_links(
        market="kr", symbol="005930", source_report_uuid=_REPORT_UUID
    )
    assert links.event_anchor is None
    assert links.alert_anchor is None
    assert links.stock_path == "/invest/stocks/kr/005930"


def test_build_invest_links_quotes_symbol() -> None:
    links = build_invest_links(
        market="us", symbol="BRK.B", source_report_uuid=_REPORT_UUID
    )
    assert links.stock_path == "/invest/stocks/us/BRK.B"


def test_operator_action_guidance_mapping() -> None:
    g = build_operator_action_guidance(action_mode="notify_only", outcome="notified")
    assert g.requires_operator_review is False
    assert g.order_behavior == "none"
    assert "자동 주문 없음" in g.headline

    g = build_operator_action_guidance(
        action_mode="approval_required", outcome="review_required"
    )
    assert g.requires_operator_review is True
    assert g.order_behavior == "none"

    g = build_operator_action_guidance(
        action_mode="preview_only", outcome="preview_attached"
    )
    assert g.order_behavior == "preview_only"

    g = build_operator_action_guidance(
        action_mode="auto_execute_mock", outcome="executed"
    )
    assert g.order_behavior == "mock_only"


def test_operator_action_guidance_review_required_overrides() -> None:
    # validity review path: notify_only watch이지만 outcome=review_required
    g = build_operator_action_guidance(
        action_mode="notify_only", outcome="review_required"
    )
    assert g.requires_operator_review is True


def _full_recommendation() -> dict:
    return {
        "watch_reason": "r",
        "data_state": "ok",
        "reference_price": "110",
        "entry_review_below_price": "100",
        "suggested_limit_price_range": {"low": "95", "high": "100"},
        "max_chase_price": "102",
        "invalidation": {"kind": "price_below", "price": "80"},
        "review_cadence": "daily",
        "source_evidence": {"lookback_days": 20},
        "policy_version": "v1",
        "computed_at": "2026-06-01T00:00:00+00:00",
    }


def test_price_guidance_extracts_advisory_subset() -> None:
    guidance = price_guidance_from_watch_recommendation(_full_recommendation())
    assert guidance is not None
    assert guidance.entry_review_below_price == Decimal("100")
    assert guidance.suggested_limit_price_range is not None
    assert guidance.suggested_limit_price_range.low == Decimal("95")
    assert guidance.suggested_limit_price_range.high == Decimal("100")
    assert guidance.max_chase_price == Decimal("102")
    assert guidance.invalidation is not None
    assert guidance.invalidation.kind == "price_below"
    assert guidance.invalidation.price == Decimal("80")


def test_price_guidance_none_when_recommendation_missing() -> None:
    assert price_guidance_from_watch_recommendation(None) is None
    assert price_guidance_from_watch_recommendation("not-a-dict") is None  # type: ignore[arg-type]


def test_price_guidance_none_when_all_advisory_fields_absent() -> None:
    rec = _full_recommendation()
    for key in (
        "entry_review_below_price",
        "suggested_limit_price_range",
        "max_chase_price",
        "invalidation",
    ):
        rec[key] = None
    assert price_guidance_from_watch_recommendation(rec) is None


def test_price_guidance_none_when_subset_malformed() -> None:
    rec = _full_recommendation()
    rec["suggested_limit_price_range"] = {"low": "100", "high": "90"}  # low > high
    assert price_guidance_from_watch_recommendation(rec) is None


def test_payload_accepts_new_optional_fields_and_still_forbids_extras() -> None:
    payload = _base_payload()  # 기존 헬퍼 (tests/test_hermes_client.py:18)
    assert payload.invest_links is None
    assert payload.operator_action_guidance is None
    assert payload.price_guidance is None
    with pytest.raises(ValidationError):
        _base_payload(unknown_field=1)


def test_planned_action_from_max_action_maps_canonical_keys() -> None:
    action = planned_action_from_max_action(
        {
            "side": "buy",
            "quantity": "1",
            "amount_krw": "980000",
            "limit_price": "975000",
            "ladder_level": "1",
        }
    )

    assert action is not None
    assert action.side == "buy"
    assert action.qty == Decimal("1")
    assert action.amount_krw == Decimal("980000")
    assert action.limit_price_hint == Decimal("975000")
    assert action.ladder_level == "1"


def test_planned_action_from_max_action_prefers_explicit_aliases() -> None:
    action = planned_action_from_max_action(
        {
            "side": "buy",
            "qty": "2",
            "quantity": "1",
            "amount_krw": "1900000",
            "limit_price_hint": "955000",
            "limit_price": "975000",
        }
    )

    assert action is not None
    assert action.qty == Decimal("2")
    assert action.limit_price_hint == Decimal("955000")


def test_planned_action_from_max_action_none_for_empty_or_malformed() -> None:
    assert planned_action_from_max_action({}) is None
    assert planned_action_from_max_action(None) is None
    assert planned_action_from_max_action({"side": "hold", "quantity": "1"}) is None
    assert planned_action_from_max_action({"side": "buy", "quantity": "oops"}) is None


def test_trigger_checklist_from_raw_returns_strings_only() -> None:
    assert trigger_checklist_from_raw(["quote", "thesis"]) == ["quote", "thesis"]
    assert trigger_checklist_from_raw(None) == []
    assert trigger_checklist_from_raw(["ok", {"bad": True}, 1]) == ["ok"]


def test_payload_accepts_planned_action_and_trigger_checklist() -> None:
    payload = _base_payload(
        planned_action={
            "side": "buy",
            "qty": "1",
            "amount_krw": "980000",
            "limit_price_hint": "975000",
            "ladder_level": "1",
        },
        trigger_checklist=["quote ok", "thesis ok"],
    )

    assert payload.planned_action is not None
    assert payload.planned_action.qty == Decimal("1")
    assert payload.trigger_checklist == ["quote ok", "thesis ok"]


@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_direct_success_maps_to_success(monkeypatch):
    from unittest.mock import AsyncMock

    fake = AsyncMock()
    fake.notify_investment_watch = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier", lambda: fake
    )
    client = HermesNotificationClient()
    res = await client.send_review_trigger(_base_payload())
    assert res.status == "success"
    fake.notify_investment_watch.assert_awaited_once()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_direct_failure_maps_to_skipped(monkeypatch):
    from unittest.mock import AsyncMock

    fake = AsyncMock()
    fake.notify_investment_watch = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier", lambda: fake
    )
    res = await HermesNotificationClient().send_review_trigger(_base_payload())
    assert res.status == "skipped"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_python_direct_exception_maps_to_failed(monkeypatch):
    from unittest.mock import AsyncMock

    fake = AsyncMock()
    fake.notify_investment_watch = AsyncMock(side_effect=RuntimeError("dispatch error"))
    monkeypatch.setattr(
        "app.monitoring.trade_notifier.get_trade_notifier", lambda: fake
    )
    res = await HermesNotificationClient().send_review_trigger(_base_payload())
    assert res.status == "failed"
