"""ROB-265 — deterministic idempotency key composer tests."""

from __future__ import annotations

from app.services.investment_reports.idempotency import (
    canonical_watch_condition_hash,
    item_key,
    report_key,
    watch_activation_key,
    watch_event_key,
)


def test_report_key_is_stable() -> None:
    a = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    b = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert a == b == "report:kr_morning:kr:regular:2026-05-18:v1"


def test_report_key_none_slot() -> None:
    key = report_key(
        report_type="crypto_morning",
        market="crypto",
        market_session=None,
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert key == "report:crypto_morning:crypto:_:2026-05-18:v1"


def test_canonical_hash_is_order_independent() -> None:
    a = canonical_watch_condition_hash(
        {"metric": "rsi", "operator": "below", "threshold": 30}
    )
    b = canonical_watch_condition_hash(
        {"threshold": 30, "operator": "below", "metric": "rsi"}
    )
    assert a == b
    assert len(a) == 16


def test_canonical_hash_changes_on_value_change() -> None:
    a = canonical_watch_condition_hash({"metric": "rsi", "threshold": 30})
    b = canonical_watch_condition_hash({"metric": "rsi", "threshold": 31})
    assert a != b


def test_item_key_with_and_without_condition() -> None:
    with_cond = item_key(
        report_uuid="REPORT-UUID",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "operator": "below", "threshold": 30},
    )
    without_cond = item_key(
        report_uuid="REPORT-UUID",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        watch_condition=None,
    )
    assert with_cond.startswith(
        "item:report-uuid:watch:005930:_:trend_recovery_review:"
    )
    assert without_cond == "item:report-uuid:action:005930:buy:buy_review:_"


def test_item_key_condition_change_changes_key() -> None:
    a = item_key(
        report_uuid="R",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 30},
    )
    b = item_key(
        report_uuid="R",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 31},
    )
    assert a != b


def test_watch_activation_key() -> None:
    assert (
        watch_activation_key(source_item_uuid="ITEM-UUID") == "activation:item-uuid"
    )


def test_watch_event_key() -> None:
    assert (
        watch_event_key(
            alert_uuid="ALERT-UUID", kst_date="2026-05-18", threshold_key="70000"
        )
        == "event:alert-uuid:2026-05-18:70000"
    )
