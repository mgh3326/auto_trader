from __future__ import annotations

import json

import pytest

from app.services.watch_intent_policy import (
    IntentPolicy,
    NotifyOnlyPolicy,
    WatchPolicyError,
    parse_policy,
)


def _payload(**fields: object) -> str:
    return json.dumps({"created_at": "2026-05-04T00:00:00+09:00", **fields})


class TestParsePolicyBackwardCompat:
    def test_legacy_created_at_only_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload='{"created_at":"2026-05-04T00:00:00+09:00"}',
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_missing_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="crypto",
            target_kind="asset",
            condition_type="price_above",
            raw_payload=None,
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_unparsable_payload_is_notify_only(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload="not-json",
        )
        assert isinstance(policy, NotifyOnlyPolicy)

    def test_action_notify_only_explicit(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(action="notify_only"),
        )
        assert isinstance(policy, NotifyOnlyPolicy)


class TestParsePolicyNotifyOnlyStrict:
    @pytest.mark.parametrize(
        "extra",
        [
            {"side": "buy"},
            {"quantity": 1},
            {"notional_krw": 100000},
            {"limit_price": 70000},
            {"max_notional_krw": 1500000},
        ],
    )
    def test_notify_only_with_extra_field_rejected(self, extra: dict) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="notify_only", **extra),
            )
        assert excinfo.value.code == "notify_only_must_be_bare"


class TestParsePolicyIntentMarketCondition:
    def test_crypto_market_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="crypto",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=1),
            )
        assert excinfo.value.code == "intent_market_unsupported"

    @pytest.mark.parametrize("condition_type", ["rsi_above", "rsi_below", "trade_value_above", "trade_value_below"])
    def test_non_price_condition_rejected(self, condition_type: str) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type=condition_type,
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=1),
            )
        assert excinfo.value.code == "intent_condition_unsupported"


class TestParsePolicyIntentSideAndSizing:
    def test_side_required(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", quantity=1),
            )
        assert excinfo.value.code == "intent_side_invalid"

    def test_side_invalid_value(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="long", quantity=1),
            )
        assert excinfo.value.code == "intent_side_invalid"

    def test_quantity_and_notional_both_present_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, notional_krw=100000
                ),
            )
        assert excinfo.value.code == "intent_sizing_xor"

    def test_neither_quantity_nor_notional_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy"),
            )
        assert excinfo.value.code == "intent_sizing_xor"

    def test_us_with_notional_krw_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="us",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", notional_krw=1000000
                ),
            )
        assert excinfo.value.code == "intent_us_notional_krw_unsupported"

    @pytest.mark.parametrize("bad_qty", [0, -1, 0.5, "1"])
    def test_quantity_must_be_positive_integer(self, bad_qty: object) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(action="create_order_intent", side="buy", quantity=bad_qty),
            )
        assert excinfo.value.code == "intent_quantity_invalid"

    def test_limit_price_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, limit_price=0
                ),
            )
        assert excinfo.value.code == "intent_limit_price_invalid"

    def test_max_notional_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", quantity=1, max_notional_krw=0
                ),
            )
        assert excinfo.value.code == "intent_max_notional_invalid"

    def test_notional_krw_non_positive_rejected(self) -> None:
        with pytest.raises(WatchPolicyError) as excinfo:
            parse_policy(
                market="kr",
                target_kind="asset",
                condition_type="price_below",
                raw_payload=_payload(
                    action="create_order_intent", side="buy", notional_krw=0
                ),
            )
        assert excinfo.value.code == "intent_notional_krw_invalid"


class TestParsePolicyIntentSuccess:
    def test_kr_quantity_success(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(
                action="create_order_intent", side="buy", quantity=1, max_notional_krw=1500000
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.side == "buy"
        assert policy.quantity == 1
        assert policy.notional_krw is None
        assert policy.limit_price is None
        assert policy.max_notional_krw == 1500000

    def test_kr_notional_krw_success(self) -> None:
        policy = parse_policy(
            market="kr",
            target_kind="asset",
            condition_type="price_below",
            raw_payload=_payload(
                action="create_order_intent", side="buy", notional_krw=1000000
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.notional_krw == 1000000
        assert policy.quantity is None

    def test_us_quantity_success(self) -> None:
        policy = parse_policy(
            market="us",
            target_kind="asset",
            condition_type="price_above",
            raw_payload=_payload(
                action="create_order_intent", side="sell", quantity=10, limit_price=190.5
            ),
        )
        assert isinstance(policy, IntentPolicy)
        assert policy.side == "sell"
        assert policy.quantity == 10
        assert policy.limit_price == 190.5
