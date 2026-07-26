from pathlib import Path

import pytest
import yaml

from app.services import trading_policy_service as svc


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == "2026-07-25.1"
    assert len(stamp["content_hash"]) == 12
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == "2026-07-25.1"
    assert view["version"] == svc.policy_version_stamp()["version"]
    assert view["content_hash"] == svc.policy_content_hash()
    assert view["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert view["thresholds"]["portfolio.max_symbols_per_theme"]["value"] == 2
    assert view["thresholds"]["sell.loss_guard_min_multiple"]["value"] == 1.01
    assert "sell.rsi_place_min" not in view["thresholds"]
    assert set(view["decision_rules"]) == {
        "phase25.06_toss_account_symbol_mode",
        "phase25.07_risk_and_hard_exit_priority",
    }


def test_get_policy_for_sell_lane_filters_thresholds():
    thresholds = svc.get_policy_for("kr", "sell")["thresholds"]

    assert thresholds["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in thresholds


def test_get_policy_for_filters_crypto_market_rules_by_lane():
    buy = svc.get_policy_for("crypto", "buy")["market_rules"]
    assert set(buy) == {"recovery_gate", "support_resistance", "no_chasing"}
    discovery = svc.get_policy_for("crypto", "discovery")["market_rules"]
    assert set(discovery) == {"support_resistance", "no_chasing"}
    sell = svc.get_policy_for("crypto", "sell")["market_rules"]
    assert set(sell) == {"support_resistance"}
    assert svc.get_policy_for("kr", "buy")["market_rules"] == {}


def test_single_share_exit_is_exposed_only_for_kr_sell():
    kr_sell = svc.get_policy_for("kr", "sell")["decision_rules"]
    assert "sell.single_share_exit" in kr_sell
    assert (
        "sell.single_share_exit"
        not in svc.get_policy_for("us", "sell")["decision_rules"]
    )
    assert (
        "sell.single_share_exit"
        not in svc.get_policy_for("crypto", "sell")["decision_rules"]
    )
    assert (
        "sell.single_share_exit"
        not in svc.get_policy_for("kr", "buy")["decision_rules"]
    )


def test_existing_trim_preplace_rule_is_exactly_unchanged():
    rule = svc.get_policy_for("kr", "sell")["decision_rules"]["sell.trim_preplace"]

    assert rule == {
        "semantics": (
            "Tiers are evaluated in declared priority order and the first match wins. "
            "profit_realization is resistance-distance-independent; global exclusions "
            "apply to every tier. When resistance-near favors PLACE but upside-rich "
            "would otherwise allow WATCH, resistance proximity can pre-place only a "
            "small trim; upside richness limits size, not eligibility."
        ),
        "tiers": [
            {
                "id": "profit_realization",
                "conditions": {"profit_pct_min": 8},
                "action": "preplace_small_trim_ladder",
                "sizing": "small_trim_only",
            },
            {
                "id": "rsi_confirmed_resistance",
                "conditions": {
                    "rsi_min_policy_key": "sell.rsi_place_min",
                    "resistance_near_pct_max_policy_key": "sell.resistance_near_pct",
                },
                "action": "preplace_small_trim_ladder",
                "sizing": "small_trim_only",
            },
            {
                "id": "ultra_near_resistance",
                "conditions": {
                    "rsi_below_policy_key": "sell.rsi_place_min",
                    "resistance_near_pct_max": 2,
                },
                "action": "preplace_small_trim_ladder",
                "sizing": "small_trim_only",
            },
            {
                "id": "watch_zone",
                "conditions": {
                    "rsi_below_policy_key": "sell.rsi_place_min",
                    "resistance_near_pct_min_exclusive": 2,
                    "resistance_near_pct_max_policy_key": "sell.resistance_near_pct",
                },
                "action": "register_watch",
                "sizing": "no_preplaced_trim",
            },
        ],
        "tie_breaks": {
            "tier_priority": (
                "profit_realization > rsi_confirmed_resistance > "
                "ultra_near_resistance > watch_zone"
            ),
            "multiple_tiers_matched": "first_matching_tier_wins",
            "sell.upside_place_max_pct": "size_limit_only",
        },
        "exclusions": [
            "single_share_position",
            "no_resistance_reference",
            "composite_gates",
        ],
    }


def test_market_override_applied(monkeypatch, tmp_path):
    raw = yaml.safe_load(svc._POLICY_PATH.read_text(encoding="utf-8"))
    raw["market_overrides"]["us"]["screen.rsi_max"] = 55
    policy_path = tmp_path / "trading_policy.yaml"
    policy_path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(svc, "_POLICY_PATH", Path(policy_path))
    svc._reset_cache_for_tests()
    threshold = svc.get_policy_for("us", "discovery")["thresholds"]["screen.rsi_max"]
    assert threshold["value"] == 55
    assert threshold["source"] == "override"


@pytest.mark.parametrize(
    ("market", "lane"),
    [("us", "sell"), ("crypto", "discovery")],
)
def test_global_advisories_remain_market_lane_independent(market, lane):
    view = svc.get_policy_for(market, lane)
    assert view["crash_day"]["trigger"]["index_symbol"] == "069500"
    assert any(
        stance["id"] == "ai-demand-real-value-selective"
        for stance in view["user_stances"]
    )


def test_global_advisories_are_identical_across_market_and_lane():
    us_sell = svc.get_policy_for("us", "sell")
    crypto_discovery = svc.get_policy_for("crypto", "discovery")

    assert us_sell["crash_day"] == crypto_discovery["crash_day"]
    assert us_sell["user_stances"] == crypto_discovery["user_stances"]


@pytest.mark.parametrize("market", ["jp", "KR"])
def test_unknown_market_raises(market):
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for(market, "buy")


def test_unknown_lane_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("kr", "scalp")


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("반도체", "semis_memory"),
        ("반도체와반도체장비", "semis_memory"),
        ("Financial Services", "financials"),
        ("Drug Manufacturers—General", "bio"),
        ("의료정밀", None),
        ("Healthcare", None),
        ("Healthcare Plans", None),
        ("정체불명업종", None),
        (None, None),
    ],
)
def test_sector_cluster_mapping(label, expected):
    assert svc.sector_cluster_for(label) == expected
