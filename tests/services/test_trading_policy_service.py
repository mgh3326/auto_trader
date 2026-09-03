from pathlib import Path

import pytest
import yaml

from app.services import trading_policy_service as svc


def _policy_key_references(value: object) -> list[str]:
    references: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.endswith("_policy_key") and isinstance(child, str):
                references.append(child)
            references.extend(_policy_key_references(child))
    elif isinstance(value, list):
        for child in value:
            references.extend(_policy_key_references(child))
    return references


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == svc.load_trading_policy().version
    assert len(stamp["content_hash"]) == 12
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == svc.load_trading_policy().version
    assert view["version"] == svc.policy_version_stamp()["version"]
    assert view["content_hash"] == svc.policy_content_hash()
    assert view["thresholds"]["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert view["thresholds"]["portfolio.max_symbols_per_theme"]["value"] == 2
    assert view["thresholds"]["sell.loss_guard_min_multiple"]["value"] == 1.01
    assert "sell.rsi_place_min" not in view["thresholds"]
    assert set(view["decision_rules"]) == {
        "buy.support_reserve_net",
        "buy.preplanned_support_ladder",
        "buy.winner_pullback_add",
        # §139차 — KR/US only; the crypto held-majors tier must not appear here.
        "buy.index_etf_candidate",
    }
    reserve = view["decision_rules"]["buy.support_reserve_net"]
    assert reserve["discount_below_support_pct_range"] == [5, 10]
    assert reserve["final_limit_distance_from_current_pct_range"] == [-15, -5]
    assert reserve["fill_triage"]["same_session_rearm"] is False
    assert reserve["priority_rules"]["allocation_order"] == [
        "dedupe_active_or_resting_same_symbol",
        "first_slot_eligible_new_candidate",
        "add_secondary_pool_only_after_r931_pass_and_full_a_limit_10",
    ]
    assert reserve["priority_rules"]["same_intent_class_sort_order"] == [
        "support_strength_desc",
        "independent_support_source_count_desc",
        "honest_upside_pct_desc",
        "post_fill_sector_increase_asc",
        "required_cash_asc",
    ]
    assert reserve["priority_rules"]["exact_tie_break"] == "NEW_BEFORE_ADD"
    assert reserve["add_candidate"]["a_limit_lte_zero"] == "NO_ORDER"


def test_get_policy_for_sell_lane_filters_thresholds():
    thresholds = svc.get_policy_for("kr", "sell")["thresholds"]

    assert thresholds["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in thresholds


@pytest.mark.parametrize("market", ["kr", "us", "crypto"])
def test_sell_projection_reads_order_proposal_values_without_changing_other_lanes(
    market,
):
    policy = svc.load_trading_policy()
    auto_approve = policy.order_proposals.auto_approve
    source = "projected from order_proposals.auto_approve (§40차/§142차)"

    sell_thresholds = svc.get_policy_for(market, "sell")["thresholds"]
    assert sell_thresholds["order_proposals.auto_approve.breakeven_band_pct"] == {
        "value": auto_approve.breakeven_band_pct,
        "unit": "percent",
        "semantics": (
            "the ±% band around avg_buy_price inside which a sell is treated as a "
            "break-even boundary case and must go to a human, whatever the sign "
            "of the P&L. §40차 fixes this at 1%."
        ),
        "of": None,
        "one_share_exception": None,
        "source": source,
    }
    assert sell_thresholds["order_proposals.auto_approve.round_trip_cost_bps"] == {
        "value": auto_approve.round_trip_cost_bps[market],
        "unit": "bps",
        "semantics": (
            "total both-leg cost (commission + transaction tax + FX spread where "
            'applicable), used to net down expected realized P&L before the "> 0" '
            "test."
        ),
        "of": None,
        "one_share_exception": None,
        "source": source,
    }

    for lane in ("buy", "discovery"):
        thresholds = svc.get_policy_for(market, lane)["thresholds"]
        assert "order_proposals.auto_approve.breakeven_band_pct" not in thresholds
        assert "order_proposals.auto_approve.round_trip_cost_bps" not in thresholds


def test_all_policy_key_references_bind_to_thresholds_for_every_market_lane():
    """Every exposed policy-key reference must resolve in its lane view.

    ``phase25.10_single_share_exit_choice`` is a decision-rule id, not a
    threshold key, so it is intentionally excluded from the threshold check.
    The assertion is membership-based so removing the projection produces a
    genuine ``AssertionError`` rather than a lookup/fixture failure.
    """

    policy = svc.load_trading_policy()
    rule_ids = set(policy.decision_rules)
    all_references: set[str] = set()
    missing: list[tuple[str, str, str]] = []

    for market in ("kr", "us", "crypto"):
        for lane in ("buy", "sell", "discovery"):
            view = svc.get_policy_for(market, lane)
            references = _policy_key_references(
                {
                    "decision_rules": view["decision_rules"],
                    "market_rules": view["market_rules"],
                }
            )
            all_references.update(references)
            for reference in set(references) - rule_ids:
                if reference not in view["thresholds"]:
                    missing.append((market, lane, reference))

    assert len(all_references) == 10
    assert missing == []


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


def test_trim_preplace_exposes_d2_d5_d7_advisory_contracts():
    rule = svc.get_policy_for("kr", "sell")["decision_rules"]["sell.trim_preplace"]
    tiers = {tier["id"]: tier for tier in rule["tiers"]}

    assert list(tiers) == [
        "de_minimis_trim_watch",
        "sell.breakeven_reserve_trim",
        "single_share_full_exit_review",
        "momentum_spike_profit_ladder",
        "rsi_confirmed_resistance",
        "ultra_near_resistance",
        "watch_zone",
        "breakeven_extension_ladder",
    ]
    assert tiers["de_minimis_trim_watch"]["conditions"] == {
        "markets": ["kr", "us", "crypto"],
        "trim_candidate": True,
        "expected_net_realized_gain_krw_below_policy_key": (
            "sell.trim_min_expected_net_realized_gain_krw"
        ),
    }
    assert tiers["de_minimis_trim_watch"]["action"] == (
        "register_watch_instead_of_trim"
    )
    assert (
        tiers["single_share_full_exit_review"]["conditions"][
            "profit_pct_min_policy_key"
        ]
        == "sell.single_share_profit_pct_min"
    )
    assert tiers["single_share_full_exit_review"]["sizing"] == "full_position"
    spike = tiers["momentum_spike_profit_ladder"]
    assert spike["conditions"]["session_change_pct_min_policy_key"] == (
        "sell.momentum_spike_change_pct_min"
    )
    assert spike["conditions"]["rsi_gate_exempt"] is True
    assert spike["conditions"]["required_thesis_evidence"] == [
        "catalyst_basis",
        "flow_basis",
    ]
    assert spike["conditions"]["resistance_levels_required"] == [
        "resistance_1",
        "resistance_2",
    ]
    assert spike["conditions"]["ladder_total_position_pct_max"] == 33.3333
    assert spike["sizing"] == "at_most_one_third_position"
    assert rule["tie_breaks"]["multiple_tiers_matched"] == ("first_matching_tier_wins")
    assert rule["tie_breaks"]["momentum_spike_integer_rounding"] == (
        "floor_without_exceeding_one_third_or_watch"
    )
    assert "single_share_position" not in rule["exclusions"]
    assert rule["exclusions"] == ["no_resistance_reference", "composite_gates"]


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
