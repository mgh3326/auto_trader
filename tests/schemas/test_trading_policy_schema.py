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
    assert doc.version == "2026-07-12.1"
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
