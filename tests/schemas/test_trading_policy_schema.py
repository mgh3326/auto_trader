from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from app.schemas.trading_policy import TradingPolicyDocument

_CONFIG = Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"


def _raw() -> dict:
    return yaml.safe_load(_CONFIG.read_text(encoding="utf-8"))


def test_shipped_config_validates():
    doc = TradingPolicyDocument.model_validate(_raw())
    assert doc.version == "2026-07-17.1"
    # verbatim seed values from the playbook policy_keys
    assert doc.thresholds["portfolio.sector_cluster_cap_pct"].value == 10
    assert doc.thresholds["sell.loss_guard_min_multiple"].value == 1.01
    assert doc.thresholds["screen.rsi_max"].value == 45
    assert doc.thresholds["buy.deep_limit_pct_range"].value == [-12, -3]
    assert set(doc.market_overrides.keys()) == {"kr", "us", "crypto"}
    assert "semis_memory" in doc.sector_clusters
    assert "sell.trim_preplace" in doc.decision_rules
    trim_rule = doc.decision_rules["sell.trim_preplace"]
    assert trim_rule.lanes == ["sell"]
    assert [tier.id for tier in trim_rule.tiers] == [
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
    ]
    assert trim_rule.tiers[1].conditions["resistance_near_pct_max"] == 2
    assert trim_rule.tie_breaks["sell.upside_place_max_pct"] == "size_limit_only"


def test_auto_approve_policy_has_conservative_market_caps():
    auto = TradingPolicyDocument.model_validate(_raw()).order_proposals.auto_approve

    assert auto.min_distance_pct > 0
    assert set(auto.per_order_cap) == {"kr", "us", "crypto"}
    assert set(auto.daily_cap) == {"kr", "us", "crypto"}
    for market, per_order in auto.per_order_cap.items():
        assert per_order > 0
        assert auto.daily_cap[market] >= per_order


def test_crypto_market_rules_preserve_report_derived_and_null_thresholds():
    doc = TradingPolicyDocument.model_validate(_raw())
    rules = doc.market_rules["crypto"]
    gate = rules.recovery_gate

    assert gate.min_conditions_met == 2
    assert gate.of == 4
    assert [condition.id for condition in gate.conditions] == [
        "fear_greed",
        "alt_breadth_24h",
        "btc_long_short_ratio",
        "btc_kimchi_premium",
    ]
    assert gate.conditions[0].threshold is None
    assert (gate.conditions[1].operator, gate.conditions[1].threshold) == (
        "gt",
        50,
    )
    assert (gate.conditions[2].operator, gate.conditions[2].threshold) == (
        "lte",
        1.5,
    )
    assert gate.conditions[3].threshold is None
    assert rules.no_chasing.daily_change_pct_threshold is None
    assert rules.no_chasing.min_trade_value_24h_krw is None
    assert rules.support_resistance.source_priority == [
        "fibonacci",
        "value_area",
        "bb_lower",
        "bb_upper",
        "bb_middle",
        "volume_poc",
    ]


def test_decision_rule_schema_accepts_sell_trim_preplace_block():
    raw = _raw()
    raw["decision_rules"] = {
        "sell.trim_preplace": {
            "lanes": ["sell"],
            "semantics": "Tie-break resistance-near vs upside-rich sell signals.",
            "tiers": [
                {
                    "id": "rsi_confirmed_resistance",
                    "conditions": {
                        "rsi_min_policy_key": "sell.rsi_place_min",
                        "resistance_near_pct_max_policy_key": (
                            "sell.resistance_near_pct"
                        ),
                    },
                    "action": "preplace_small_trim_ladder",
                    "sizing": "small_trim_only",
                }
            ],
            "tie_breaks": {
                "sell.upside_place_max_pct": "size_limit_only",
            },
            "exclusions": ["single_share_position"],
        }
    }
    doc = TradingPolicyDocument.model_validate(raw)
    rule = doc.decision_rules["sell.trim_preplace"]
    assert rule.tiers[0].action == "preplace_small_trim_ladder"


def test_extra_key_rejected():
    raw = _raw()
    raw["unexpected_top_level"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_threshold_key_rejected():
    raw = _raw()
    raw["thresholds"]["screen.rsi_max"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_extra_crypto_market_rule_key_rejected():
    raw = _raw()
    raw["market_rules"]["crypto"]["no_chasing"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_trigger_and_actions_parse():
    doc = TradingPolicyDocument.model_validate(_raw())
    crash_day = doc.crash_day

    assert crash_day.trigger.index_symbol == "069500"
    assert crash_day.trigger.index_gap_pct_max == -3.0
    assert crash_day.actions.new_entry_hold is True
    assert crash_day.actions.deep_rung_reprice_to_band_floor is True
    assert crash_day.actions.profit_trim_marketable_allowed is True
    assert crash_day.actions.defensive_brief_cross_check is True


def test_crash_day_extra_trigger_key_rejected():
    raw = _raw()
    raw["crash_day"]["trigger"]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_extra_actions_key_rejected():
    raw = _raw()
    raw["crash_day"]["actions"]["bogus"] = True
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_crash_day_missing_block_rejected():
    raw = _raw()
    del raw["crash_day"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_ai_demand_selective_parses():
    doc = TradingPolicyDocument.model_validate(_raw())
    stances = {stance.id: stance for stance in doc.user_stances}
    stance = stances["ai-demand-real-value-selective"]

    assert stance.stance.startswith("AI 수요는 실사용 관점에서 실재")
    assert len(stance.implications) == 4
    assert (
        "3배 레버리지 ETF(SOXL류)는 눌림 보유 수단에서 기본 제외 (변동성 감쇠)"
        in stance.implications
    )
    assert stance.risk_scenario.startswith("효율 충격")
    assert stance.review_condition.startswith("하이퍼스케일러 AI capex 감소 가이던스")
    assert stance.review_date == "2026-10-17"


def test_user_stance_extra_key_rejected():
    raw = _raw()
    raw["user_stances"][0]["bogus"] = 1
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_missing_required_field_rejected():
    raw = _raw()
    del raw["user_stances"][0]["risk_scenario"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stance_invalid_review_date_rejected():
    raw = _raw()
    raw["user_stances"][0]["review_date"] = "not-a-date"
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)


def test_user_stances_missing_block_rejected():
    raw = _raw()
    del raw["user_stances"]
    with pytest.raises(ValidationError):
        TradingPolicyDocument.model_validate(raw)
