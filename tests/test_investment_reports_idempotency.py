"""ROB-265 — deterministic idempotency key composer tests."""

from __future__ import annotations

from app.services.investment_reports.idempotency import (
    canonical_watch_condition_hash,
    item_key,
    kst_date_from_report_key,
    report_key,
    watch_activation_key,
    watch_event_key,
)


def test_kst_date_from_report_key_round_trips() -> None:
    key = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert kst_date_from_report_key(key) == "2026-05-18"


def test_kst_date_from_report_key_handles_bad_input() -> None:
    assert kst_date_from_report_key(None) is None
    assert kst_date_from_report_key("") is None
    assert kst_date_from_report_key("not-a-report-key") is None


def test_report_key_is_stable() -> None:
    a = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    b = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert a == b == "report:kr_morning:kr:regular:kis_mock:mock_preview:2026-05-18:v1"


def test_report_key_none_slot_for_optional_fields() -> None:
    key = report_key(
        report_type="crypto_morning",
        market="crypto",
        market_session=None,
        account_scope=None,
        execution_mode="advisory_only",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert key == "report:crypto_morning:crypto:_:_:advisory_only:2026-05-18:v1"


def test_report_key_distinguishes_account_scope() -> None:
    """kis_mock vs kis_live for same date/session/generator must NOT collide."""
    mock = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_mock",
        execution_mode="mock_preview",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    live = report_key(
        report_type="kr_morning",
        market="kr",
        market_session="regular",
        account_scope="kis_live",
        execution_mode="advisory_only",
        kst_date="2026-05-18",
        generator_version="v1",
    )
    assert mock != live


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
        client_item_key="watch-1",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "operator": "below", "threshold": 30},
    )
    without_cond = item_key(
        report_uuid="REPORT-UUID",
        client_item_key="action-1",
        item_kind="action",
        symbol="005930",
        side="buy",
        intent="buy_review",
        watch_condition=None,
    )
    assert with_cond.startswith(
        "item:report-uuid:watch-1:watch:005930:_:trend_recovery_review:"
    )
    assert without_cond == ("item:report-uuid:action-1:action:005930:buy:buy_review:_")


def test_item_key_client_item_key_distinguishes_same_natural_fields() -> None:
    """Two items with identical natural fields but different client keys
    must NOT collide — fixes the multi-risk / scoped-buy collision case."""
    a = item_key(
        report_uuid="R",
        client_item_key="risk-1",
        item_kind="risk",
        symbol=None,
        side=None,
        intent="risk_review",
        watch_condition=None,
    )
    b = item_key(
        report_uuid="R",
        client_item_key="risk-2",
        item_kind="risk",
        symbol=None,
        side=None,
        intent="risk_review",
        watch_condition=None,
    )
    assert a != b


def test_item_key_condition_change_changes_key() -> None:
    a = item_key(
        report_uuid="R",
        client_item_key="watch-1",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 30},
    )
    b = item_key(
        report_uuid="R",
        client_item_key="watch-1",
        item_kind="watch",
        symbol="005930",
        side=None,
        intent="trend_recovery_review",
        watch_condition={"metric": "rsi", "threshold": 31},
    )
    assert a != b


def test_watch_activation_key() -> None:
    assert watch_activation_key(source_item_uuid="ITEM-UUID") == "activation:item-uuid"


def test_watch_event_key() -> None:
    assert (
        watch_event_key(
            alert_uuid="ALERT-UUID", kst_date="2026-05-18", threshold_key="70000"
        )
        == "event:alert-uuid:2026-05-18:70000"
    )
