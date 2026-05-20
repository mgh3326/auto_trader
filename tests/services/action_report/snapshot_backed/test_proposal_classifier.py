"""ROB-274 — proposal classifier tests."""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

from app.schemas.investment_reports import IngestReportItem, WatchConditionPayload
from app.services.action_report.snapshot_backed.proposal_classifier import (
    ClassifierContext,
    classify_items,
)


def _draft_watch_item(symbol: str, threshold: str = "100") -> IngestReportItem:
    return IngestReportItem(
        client_item_key=f"w-{symbol}",
        item_kind="watch",
        intent="trend_recovery_review",
        rationale="r",
        symbol=symbol,
        watch_condition=WatchConditionPayload(
            metric="price", operator="above", threshold=Decimal(threshold)
        ),
        valid_until=dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7),
    )


def _active_alert(symbol: str, threshold: str, operator: str = "above") -> dict:
    return {
        "alert_uuid": uuid.uuid4(),
        "symbol": symbol,
        "metric": "price",
        "operator": operator,
        "threshold": Decimal(threshold),
        "intent": "trend_recovery_review",
        "action_mode": "notify_only",
        "status": "active",
        "valid_until": dt.datetime.now(tz=dt.UTC) + dt.timedelta(days=7),
    }


def _pending_order(broker: str, symbol: str, *, stale: bool = False) -> dict:
    return {
        "target_ref": {"type": "broker_order", "broker": broker, "id": "O1", "raw": {}},
        "symbol": symbol,
        "side": "buy",
        "price": "100",
        "quantity": "1",
        "remaining_quantity": "1",
        "placed_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "stale": stale,
    }


def test_watch_no_match_becomes_create():
    classified = classify_items(
        items=[_draft_watch_item("KRW-BTC")],
        context=ClassifierContext(active_watches=[], pending_orders=[]),
    )
    assert classified[0].operation == "create"
    assert classified[0].target_ref is None
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_watch_matching_existing_active_with_same_condition_becomes_keep():
    draft = _draft_watch_item("KRW-BTC", threshold="100")
    alert = _active_alert("KRW-BTC", threshold="100", operator="above")
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=[alert], pending_orders=[]),
    )
    assert classified[0].operation == "keep"
    assert classified[0].target_ref is not None
    assert classified[0].target_ref.type == "investment_watch_alert"
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_watch_matching_existing_with_changed_threshold_becomes_modify():
    draft = _draft_watch_item("KRW-BTC", threshold="120")
    alert = _active_alert("KRW-BTC", threshold="100", operator="above")
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=[alert], pending_orders=[]),
    )
    assert classified[0].operation == "modify"
    assert classified[0].diff is not None
    assert any(d["field"] == "threshold" for d in classified[0].diff)
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_multiple_ambiguous_watches_become_review():
    draft = _draft_watch_item("KRW-BTC", threshold="100")
    alerts = [
        _active_alert("KRW-BTC", threshold="100"),
        _active_alert("KRW-BTC", threshold="100"),
    ]
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=alerts, pending_orders=[]),
    )
    assert classified[0].operation == "review"
    assert classified[0].target_ref.type == "ambiguous"
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_buy_action_with_matching_open_order_keep():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(
            active_watches=[],
            pending_orders=[_pending_order("upbit", "KRW-BTC")],
        ),
    )
    assert classified[0].operation == "keep"
    assert classified[0].target_ref.type == "broker_order"
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_buy_action_with_stale_open_order_becomes_review_with_confirmation_note():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(
            active_watches=[],
            pending_orders=[_pending_order("upbit", "KRW-BTC", stale=True)],
        ),
    )
    assert classified[0].operation == "review"
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())


def test_pending_orders_unavailable_marks_dependent_items_review():
    draft = IngestReportItem(
        client_item_key="a-1",
        item_kind="action",
        intent="buy_review",
        rationale="r",
        symbol="KRW-BTC",
        side="buy",
    )
    classified = classify_items(
        items=[draft],
        context=ClassifierContext(active_watches=[], pending_orders=None),
    )
    # When pending_orders snapshot is missing, item must be downgraded
    # to action/review with explicit unknown note.
    assert classified[0].operation == "review"
    assert "확인 불가" in classified[0].rationale
    # Roundtrip safety: classified output must survive re-validation.
    for item in classified:
        IngestReportItem.model_validate(item.model_dump())
