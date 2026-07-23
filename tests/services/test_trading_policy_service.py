import inspect
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.schemas.trading_policy import (
    SingleShareExitAccountLot,
    SingleShareExitBrokerAccountSnapshot,
    SingleShareExitEvidenceSnapshot,
    SingleShareExitOpenAction,
    SingleShareExitOpenOrder,
    SingleShareExitQuoteEvidence,
    SingleShareExitResistanceEvidence,
    SingleShareExitTargetIdentity,
)
from app.services import trading_policy_service as svc


def test_version_stamp_has_version_and_hash():
    stamp = svc.policy_version_stamp()
    assert stamp["version"] == "2026-07-23.2"
    assert len(stamp["content_hash"]) == 12


def test_content_hash_stable_across_calls():
    assert svc.policy_content_hash() == svc.policy_content_hash()


def test_get_policy_for_buy_kr_includes_cap_and_version():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == "2026-07-23.2"
    assert view["content_hash"]
    t = view["thresholds"]
    # buy lane references these (playbook lane tags)
    assert t["portfolio.sector_cluster_cap_pct"]["value"] == 10
    assert t["portfolio.sector_cluster_cap_pct"]["source"] == "default"
    assert t["portfolio.max_symbols_per_theme"]["value"] == 2
    assert t["recovery_gate.min_conditions_met"]["value"] == 2
    assert t["recovery_gate.min_conditions_met"]["of"] == 2
    assert t["sell.loss_guard_min_multiple"]["value"] == 1.01
    # sell-only threshold must NOT appear in the buy lane
    assert "sell.rsi_place_min" not in t
    assert view["decision_rules"] == {}


def test_get_policy_for_crypto_buy_exposes_report_derived_market_rules():
    view = svc.get_policy_for("crypto", "buy")

    assert view["version"] == "2026-07-23.2"
    assert set(view["market_rules"]) == {
        "recovery_gate",
        "support_resistance",
        "no_chasing",
    }
    gate = view["market_rules"]["recovery_gate"]
    assert gate["min_conditions_met"] == 2
    assert gate["of"] == 2
    assert [condition["id"] for condition in gate["conditions"]] == [
        "alt_breadth_24h",
        "btc_long_short_ratio",
    ]
    assert [context["id"] for context in gate["advisory_context"]] == [
        "fear_greed",
        "btc_kimchi_premium",
    ]
    assert "lanes" not in gate
    assert view["market_rules"]["no_chasing"]["daily_change_pct_threshold"] is None


def test_get_policy_for_filters_crypto_market_rules_by_lane():
    discovery = svc.get_policy_for("crypto", "discovery")["market_rules"]
    assert set(discovery) == {"support_resistance", "no_chasing"}

    sell = svc.get_policy_for("crypto", "sell")["market_rules"]
    assert set(sell) == {"support_resistance"}

    assert svc.get_policy_for("kr", "buy")["market_rules"] == {}


def test_get_policy_for_sell_lane_has_sell_keys():
    view = svc.get_policy_for("kr", "sell")
    t = view["thresholds"]
    assert t["sell.rsi_place_min"]["value"] == 58
    assert "screen.rsi_max" not in t
    rule = view["decision_rules"]["sell.trim_preplace"]
    assert rule["tiers"][0]["id"] == "profit_realization"
    assert rule["tiers"][0]["conditions"]["profit_pct_min"] == 8
    assert rule["tiers"][1]["conditions"]["rsi_min_policy_key"] == (
        "sell.rsi_place_min"
    )
    assert rule["tiers"][2]["conditions"]["resistance_near_pct_max"] == 2
    assert rule["tiers"][3]["action"] == "register_watch"
    assert rule["tie_breaks"]["sell.upside_place_max_pct"] == "size_limit_only"


_NOW = datetime(2026, 7, 23, 7, 0, tzinfo=UTC)
_SNAPSHOT_AT = _NOW - timedelta(seconds=60)
_EXPECTED_KRX_BAR = date(2026, 7, 23)


@pytest.fixture(autouse=True)
def _stub_expected_completed_krx_bar(monkeypatch):
    monkeypatch.setattr(
        svc, "_expected_completed_krx_bar", lambda _now: _EXPECTED_KRX_BAR
    )


def _lot(
    *,
    symbol="257720",
    lot_id="lot-1",
    quantity="1",
    average_cost="100000",
    order_routable=True,
):
    return SingleShareExitAccountLot(
        symbol=symbol,
        lot_id=lot_id,
        order_routable=order_routable,
        sellable_quantity=Decimal(quantity),
        average_cost=Decimal(average_cost),
    )


def _account(
    *,
    broker,
    account_id,
    lots=None,
    orders=None,
    snapshot_id="snapshot-1",
    observed_at=_SNAPSHOT_AT,
):
    return SingleShareExitBrokerAccountSnapshot(
        snapshot_id=snapshot_id,
        broker=broker,
        broker_account_id=account_id,
        observed_at=observed_at,
        holdings_complete=True,
        lots=lots or [],
        open_orders_checked_at=observed_at,
        open_orders_complete=True,
        open_orders=orders or [],
    )


def _single_share_evidence(
    *,
    symbol="257720",
    target_broker="kis",
    target_account_id="kis-main",
    target_lot_id="lot-1",
    accounts=None,
    average_cost="100000",
    quote_price="108000",
    resistance_price="119000",
    resistance_sources=None,
    resistance_strength="strong",
    snapshot_id="snapshot-1",
    quote_snapshot_id=None,
    quote_observed_at=_SNAPSHOT_AT,
    resistance_computed_at=_SNAPSHOT_AT,
    ohlcv_through_date=_EXPECTED_KRX_BAR,
    captured_at=_SNAPSHOT_AT,
    open_actions=None,
):
    if accounts is None:
        target_lot = _lot(
            symbol=symbol,
            lot_id=target_lot_id,
            average_cost=average_cost,
        )
        if target_broker == "kis":
            accounts = [
                _account(broker="kis", account_id=target_account_id, lots=[target_lot]),
                _account(broker="toss", account_id="toss-main"),
            ]
        else:
            accounts = [
                _account(broker="kis", account_id="kis-main"),
                _account(
                    broker="toss", account_id=target_account_id, lots=[target_lot]
                ),
            ]
    return SingleShareExitEvidenceSnapshot(
        snapshot_id=snapshot_id,
        market="kr",
        captured_at=captured_at,
        target=SingleShareExitTargetIdentity(
            symbol=symbol,
            broker=target_broker,
            broker_account_id=target_account_id,
            lot_id=target_lot_id,
        ),
        broker_account_scope_complete=True,
        accounts=accounts,
        quote=SingleShareExitQuoteEvidence(
            snapshot_id=quote_snapshot_id or snapshot_id,
            symbol=symbol,
            price=Decimal(quote_price),
            observed_at=quote_observed_at,
            source="kis_quote",
        ),
        resistance=SingleShareExitResistanceEvidence(
            snapshot_id=snapshot_id,
            symbol=symbol,
            price=Decimal(resistance_price),
            sources=resistance_sources or ["bb_upper", "fib_50"],
            strength=resistance_strength,
            computed_at=resistance_computed_at,
            ohlcv_through_date=ohlcv_through_date,
        ),
        open_actions_snapshot_id=snapshot_id,
        open_actions_checked_at=captured_at,
        open_actions_complete=True,
        open_actions=open_actions or [],
    )


@pytest.mark.parametrize(
    ("broker", "account_id"),
    [("kis", "kis-main"), ("toss", "toss-main")],
)
def test_single_share_exit_is_shadow_only_for_kis_and_toss(broker, account_id):
    evidence = _single_share_evidence(
        target_broker=broker,
        target_account_id=account_id,
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert result.outcome == "SHADOW_ELIGIBLE"
    assert result.outcome != "PROPOSE"
    assert result.activation_state == "shadow"
    assert result.proposal_enabled is False
    assert result.candidate_action == "propose_full_account_lot_exit"
    assert result.sizing == "full_account_lot_exit"
    assert result.approval == "telegram_manual"
    assert result.auto_approve is False
    assert result.execution == "proposal_only"


def test_evaluator_rejects_even_bypassed_proposal_enabled_policy(monkeypatch):
    doc = svc.load_trading_policy()
    rule = doc.decision_rules["sell.single_share_exit"]
    bypassed_rule = rule.model_copy(update={"proposal_enabled": True})
    bypassed_doc = doc.model_copy(
        update={
            "decision_rules": {
                **doc.decision_rules,
                "sell.single_share_exit": bypassed_rule,
            }
        }
    )
    monkeypatch.setattr(svc, "load_trading_policy", lambda: bypassed_doc)

    result = svc.evaluate_single_share_exit(_single_share_evidence(), evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "policy_not_shadow_off",
    )
    assert result.candidate_action is None


@pytest.mark.parametrize(
    ("symbol", "quote", "resistance", "sources"),
    [
        ("257720", "36450", "39946.31", ["bb_upper", "fib_50"]),
        (
            "042660",
            "89400",
            "100548.9",
            ["fib_38.2", "volume_value_area_low"],
        ),
        ("086790", "130500", "142264.52", ["bb_upper", "fib_0"]),
    ],
)
def test_measured_far_lane_two_family_cases_are_shadow_eligible(
    symbol, quote, resistance, sources
):
    # All measured lots were comfortably above 8%; avoid manufacturing an
    # exactly-8% repeating Decimal at the boundary in this evidence test.
    average_cost = Decimal(quote) / Decimal("1.10")
    evidence = _single_share_evidence(
        symbol=symbol,
        average_cost=str(average_cost),
        quote_price=quote,
        resistance_price=resistance,
        resistance_sources=sources,
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert result.outcome == "SHADOW_ELIGIBLE"
    assert len(result.normalized_source_families) == 2


def test_naver_one_family_is_ineligible_even_if_symbol_total_were_one():
    evidence = _single_share_evidence(
        symbol="035420",
        average_cost="196428.5714285714",
        quote_price="220000",
        resistance_price="242550",
        resistance_sources=["fib_50"],
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "insufficient_independent_resistance_families",
    )
    assert result.normalized_source_families == ("FIBONACCI",)


def test_naver_toss_one_plus_kis_three_is_not_single_symbol_quantity():
    accounts = [
        _account(
            broker="kis",
            account_id="kis-main",
            lots=[
                _lot(
                    symbol="035420",
                    lot_id="kis-lot",
                    quantity="3",
                    average_cost="200000",
                )
            ],
        ),
        _account(
            broker="toss",
            account_id="toss-main",
            lots=[
                _lot(
                    symbol="035420",
                    lot_id="toss-lot",
                    quantity="1",
                    average_cost="196428.5714285714",
                )
            ],
        ),
    ]
    evidence = _single_share_evidence(
        symbol="035420",
        target_broker="toss",
        target_account_id="toss-main",
        target_lot_id="toss-lot",
        accounts=accounts,
        quote_price="220000",
        resistance_price="242550",
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "symbol_routable_sellable_quantity_not_one",
    )
    assert result.symbol_routable_sellable_quantity == Decimal("4")


def test_missing_kis_or_toss_inventory_is_ineligible():
    evidence = _single_share_evidence(
        accounts=[
            _account(
                broker="kis",
                account_id="kis-main",
                lots=[_lot()],
            ),
            _account(broker="kis", account_id="kis-secondary"),
        ]
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "incomplete_kis_toss_inventory",
    )


def test_duplicate_broker_account_snapshot_is_ineligible():
    evidence = _single_share_evidence(
        accounts=[
            _account(
                broker="kis",
                account_id="kis-main",
                lots=[_lot()],
            ),
            _account(broker="kis", account_id="kis-main"),
            _account(broker="toss", account_id="toss-main"),
        ]
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "duplicate_broker_account_snapshot",
    )


def test_same_symbol_broker_open_order_defers():
    order = SingleShareExitOpenOrder(order_id="order-1", symbol="257720", side="buy")
    accounts = [
        _account(broker="kis", account_id="kis-main", lots=[_lot()]),
        _account(
            broker="toss",
            account_id="toss-main",
            orders=[order],
        ),
    ]

    result = svc.evaluate_single_share_exit(
        _single_share_evidence(accounts=accounts),
        evaluated_at=_NOW,
    )

    assert (result.outcome, result.reason) == (
        "DEFER",
        "same_symbol_broker_open_order",
    )


def test_open_action_is_scoped_by_symbol_side_and_account():
    unrelated = [
        SingleShareExitOpenAction(
            action_id="wrong-symbol",
            symbol="035420",
            side="sell",
            broker_account_id="kis-main",
            status="open",
        ),
        SingleShareExitOpenAction(
            action_id="wrong-side",
            symbol="257720",
            side="buy",
            broker_account_id="kis-main",
            status="open",
        ),
        SingleShareExitOpenAction(
            action_id="wrong-account",
            symbol="257720",
            side="sell",
            broker_account_id="toss-main",
            status="in_progress",
        ),
    ]
    unrelated_result = svc.evaluate_single_share_exit(
        _single_share_evidence(open_actions=unrelated),
        evaluated_at=_NOW,
    )
    assert unrelated_result.outcome == "SHADOW_ELIGIBLE"

    scoped = [
        *unrelated,
        SingleShareExitOpenAction(
            action_id="scoped",
            symbol="257720",
            side="sell",
            broker_account_id="kis-main",
            status="open",
        ),
    ]
    scoped_result = svc.evaluate_single_share_exit(
        _single_share_evidence(open_actions=scoped),
        evaluated_at=_NOW,
    )
    assert (scoped_result.outcome, scoped_result.reason) == (
        "DEFER",
        "unresolved_scoped_open_action",
    )


def test_stale_quote_is_ineligible():
    stale_at = _NOW - timedelta(seconds=301)
    evidence = _single_share_evidence(
        captured_at=stale_at,
        quote_observed_at=stale_at,
        resistance_computed_at=stale_at,
        accounts=[
            _account(
                broker="kis",
                account_id="kis-main",
                lots=[_lot()],
                observed_at=stale_at,
            ),
            _account(
                broker="toss",
                account_id="toss-main",
                observed_at=stale_at,
            ),
        ],
    )

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == ("INELIGIBLE", "stale_quote")


def test_inconsistent_snapshot_id_is_ineligible():
    evidence = _single_share_evidence(quote_snapshot_id="other-snapshot")

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "inconsistent_snapshot_id",
    )


def test_ohlcv_must_cover_expected_completed_krx_bar():
    evidence = _single_share_evidence(ohlcv_through_date=date(2026, 7, 22))

    result = svc.evaluate_single_share_exit(evidence, evaluated_at=_NOW)

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "ohlcv_not_through_expected_completed_krx_bar",
    )
    assert result.expected_completed_krx_bar_date == _EXPECTED_KRX_BAR


def test_profit_and_distance_are_recomputed_from_decimal_snapshot():
    parameters = inspect.signature(svc.evaluate_single_share_exit).parameters
    assert "profit_pct" not in parameters
    assert "resistance_distance_pct" not in parameters

    result = svc.evaluate_single_share_exit(
        _single_share_evidence(
            average_cost="100000",
            quote_price="108000",
            resistance_price="118800",
        ),
        evaluated_at=_NOW,
    )

    assert result.profit_pct == Decimal("8.0000")
    assert result.resistance_distance_pct == Decimal("10.0000")
    assert result.average_cost == Decimal("100000")
    assert result.current_quote == Decimal("108000")
    assert result.resistance_price == Decimal("118800")


@pytest.mark.parametrize(
    ("resistance_price", "expected_outcome"),
    [
        ("114480", "INELIGIBLE"),  # exactly +6%, exclusive
        ("124200", "SHADOW_ELIGIBLE"),  # exactly +15%, inclusive
        ("124200.01", "INELIGIBLE"),  # above +15%
    ],
)
def test_far_resistance_band_boundaries(resistance_price, expected_outcome):
    result = svc.evaluate_single_share_exit(
        _single_share_evidence(resistance_price=resistance_price),
        evaluated_at=_NOW,
    )

    assert result.outcome == expected_outcome


def test_multiple_fibonacci_sources_count_as_one_family():
    result = svc.evaluate_single_share_exit(
        _single_share_evidence(resistance_sources=["fib_0", "fib_38.2", "fib_50"]),
        evaluated_at=_NOW,
    )

    assert (result.outcome, result.reason) == (
        "INELIGIBLE",
        "insufficient_independent_resistance_families",
    )
    assert result.normalized_source_families == ("FIBONACCI",)


def test_loss_guard_is_decimal_and_checked_separately():
    result = svc.evaluate_single_share_exit(
        _single_share_evidence(
            average_cost="100000",
            quote_price="100999.9999",
            resistance_price="112000",
        ),
        evaluated_at=_NOW,
    )

    assert (result.outcome, result.reason) == ("INELIGIBLE", "loss_guard_not_met")


def test_resistance_provenance_and_freshness_fields_are_preserved():
    result = svc.evaluate_single_share_exit(
        _single_share_evidence(
            resistance_sources=["volume_poc", "bb_upper"],
            resistance_strength="moderate",
        ),
        evaluated_at=_NOW,
    )

    assert result.outcome == "SHADOW_ELIGIBLE"
    assert result.resistance_sources == ("volume_poc", "bb_upper")
    assert result.normalized_source_families == (
        "BOLLINGER",
        "VOLUME_PROFILE",
    )
    assert result.resistance_strength == "moderate"
    assert result.quote_source == "kis_quote"
    assert result.quote_age_seconds == Decimal("60.0")
    assert result.quote_observed_at == _SNAPSHOT_AT
    assert result.resistance_computed_at == _SNAPSHOT_AT
    assert result.ohlcv_through_date == _EXPECTED_KRX_BAR


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
                    "resistance_near_pct_max_policy_key": ("sell.resistance_near_pct"),
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
                    "resistance_near_pct_max_policy_key": ("sell.resistance_near_pct"),
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


def test_unknown_market_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("jp", "buy")


def test_unknown_lane_raises():
    with pytest.raises(svc.TradingPolicyKeyError):
        svc.get_policy_for("kr", "scalp")


def test_market_override_applied(monkeypatch, tmp_path):
    from pathlib import Path

    import yaml

    raw = yaml.safe_load(svc._POLICY_PATH.read_text(encoding="utf-8"))
    raw["market_overrides"]["us"]["screen.rsi_max"] = 55
    p = tmp_path / "trading_policy.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    monkeypatch.setattr(svc, "_POLICY_PATH", Path(p))
    svc.load_trading_policy.cache_clear() if hasattr(
        svc.load_trading_policy, "cache_clear"
    ) else None
    svc._reset_cache_for_tests()
    t = svc.get_policy_for("us", "discovery")["thresholds"]
    assert t["screen.rsi_max"]["value"] == 55
    assert t["screen.rsi_max"]["source"] == "override"


def test_get_policy_for_includes_crash_day_advisory_with_version_stamp():
    view = svc.get_policy_for("kr", "buy")
    assert view["version"] == svc.policy_version_stamp()["version"]
    assert view["content_hash"] == svc.policy_content_hash()
    crash_day = view["crash_day"]
    assert crash_day["trigger"]["index_symbol"] == "069500"
    assert crash_day["trigger"]["index_gap_pct_max"] == -3.0
    assert crash_day["actions"]["new_entry_hold"] is True


def test_get_policy_for_crash_day_present_regardless_of_market_lane():
    # crash_day is a single global advisory trigger, not market/lane-scoped.
    us_sell = svc.get_policy_for("us", "sell")["crash_day"]
    crypto_discovery = svc.get_policy_for("crypto", "discovery")["crash_day"]
    assert us_sell == crypto_discovery


def test_get_policy_for_includes_user_stances_advisory():
    view = svc.get_policy_for("kr", "buy")
    stances = {s["id"]: s for s in view["user_stances"]}
    stance = stances["ai-demand-real-value-selective"]
    assert stance["review_date"] == "2026-10-17"
    assert stance["risk_scenario"].startswith("효율 충격")


def test_get_policy_for_user_stances_present_regardless_of_market_lane():
    # user_stances is global advisory context, not market/lane-scoped.
    us_sell = svc.get_policy_for("us", "sell")["user_stances"]
    crypto_discovery = svc.get_policy_for("crypto", "discovery")["user_stances"]
    assert us_sell == crypto_discovery


def test_get_policy_for_includes_us_notional_usd_range_with_one_share_exception():
    view = svc.get_policy_for("us", "buy")
    t = view["thresholds"]
    us_range = t["buy.per_symbol_notional_usd_range"]

    assert us_range["value"] == [150, 450]
    assert us_range["unit"] == "usd"
    assert us_range["one_share_exception"] == {
        "enabled": True,
        "absolute_ceiling_usd": 700,
        "max_deep_rungs": 1,
    }


def test_get_policy_for_kr_notional_krw_range_has_no_one_share_exception():
    view = svc.get_policy_for("kr", "buy")
    kr_range = view["thresholds"]["buy.per_symbol_notional_krw_range"]

    assert kr_range["value"] == [200000, 400000]
    assert kr_range["one_share_exception"] is None


def test_sector_cluster_for():
    assert svc.sector_cluster_for("반도체") == "semis_memory"
    assert svc.sector_cluster_for("Financial Services") == "financials"
    assert svc.sector_cluster_for("정체불명업종") is None
    assert svc.sector_cluster_for(None) is None


def test_sector_cluster_for_no_cjk_substring_false_positive():
    # ROB-646 Finding 3: "의료" must not spill into unrelated 업종 like
    # "의료정밀" (medical *precision instruments*). Member "의료" removed +
    # matcher is one-directional (member is a substring of the label, not the
    # reverse), so "의료정밀" resolves to no cluster.
    assert svc.sector_cluster_for("의료정밀") is None


def test_sector_cluster_for_broad_healthcare_not_bio():
    # ROB-646 Finding 3: a generic "Healthcare" sector (e.g. managed-care /
    # health insurers) must not be bucketed as bio.
    assert svc.sector_cluster_for("Healthcare") is None
    assert svc.sector_cluster_for("Healthcare Plans") is None


def test_sector_cluster_for_prefix_coverage_preserved():
    # One-directional match still gives real KR coverage: the Naver 업종
    # "반도체와반도체장비" contains the member "반도체".
    assert svc.sector_cluster_for("반도체와반도체장비") == "semis_memory"
    # yfinance em-dash variants still map (member is a substring of the label).
    assert svc.sector_cluster_for("Drug Manufacturers—General") == "bio"
