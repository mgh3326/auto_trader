from __future__ import annotations

import json
import random
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    build_auto_approved_message,
    evaluate_auto_approve_eligibility,
    find_approval_required_tag_matches,
    find_approval_required_tags,
    limits_for_market,
)
from app.services.order_proposals.auto_approve_audit import (
    append_auto_approve_rejection_attempt,
    project_auto_approve_cap_observations,
    project_auto_approve_rejections,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    build_proposal_dispatch_binding,
)
from app.services.order_proposals.service import RungInput


def _group(**overrides):
    values = {
        "market": "equity_kr",
        "account_mode": "kis_live",
        "broker_account_id": "acct-1",
        "order_type": "limit",
        "action": "place",
        "exit_intent": None,
        "thesis": "test thesis",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _rung(**overrides):
    values = {
        "rung_index": 0,
        "side": "buy",
        "limit_price": Decimal("97000"),
        "quantity": Decimal("2"),
        "notional": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


_LIMITS = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("200000"),
    daily_cap=Decimal("500000"),
    policy_version="test-policy",
)


def test_limits_policy_content_hash_preserves_legacy_positional_order():
    limits = AutoApproveLimits(
        Decimal("3"),
        Decimal("200000"),
        Decimal("500000"),
        "test-policy",
        "expanded",
        Decimal("1"),
        Decimal("200"),
    )

    assert limits.mode == "expanded"
    assert limits.round_trip_cost_bps == Decimal("200")
    assert limits.policy_content_hash is None


def test_buy_at_distance_and_daily_cap_boundary_is_eligible():
    decision = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("306000"),
    )

    assert decision.eligible is True
    assert decision.reason == "eligible"
    assert decision.details["policy_version"] == "test-policy"
    assert decision.details["daily_notional_after"] == "500000"


def test_sell_requires_distance_and_previewed_loss_guard():
    eligible = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(side="sell", limit_price=Decimal("103000"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )
    blocked = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(side="sell", limit_price=Decimal("103000"), quantity=Decimal("1")),
        preview={
            "success": False,
            "current_price": "100000",
            "error": "sell price below average purchase price floor",
        },
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )

    assert eligible.eligible is True
    assert eligible.details["loss_guard"] == "preview_passed"
    assert blocked.eligible is False
    assert blocked.reason == "preview_guard_failed"


@pytest.mark.parametrize(
    ("group_overrides", "rung_overrides", "expected_reason"),
    [
        ({"order_type": "market"}, {}, "order_type_not_limit"),
        ({"action": "replace"}, {}, "action_not_place"),
        ({"action": "cancel"}, {}, "action_not_place"),
        ({"exit_intent": "loss_cut"}, {"side": "sell"}, "loss_cut_intent"),
        (
            {"exit_intent": "unknown_future_intent"},
            {"side": "sell"},
            "exit_intent_present",
        ),
        ({"account_mode": "toss_live"}, {}, "account_not_veto_capable"),
        ({}, {"limit_price": Decimal("98000")}, "distance_below_minimum"),
        ({}, {"quantity": Decimal("3")}, "per_order_cap_exceeded"),
    ],
)
def test_ineligible_orders_fail_closed(
    group_overrides, rung_overrides, expected_reason
):
    decision = evaluate_auto_approve_eligibility(
        group=_group(**group_overrides),
        rung=_rung(**rung_overrides),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == expected_reason


def test_daily_cap_one_unit_over_boundary_is_ineligible():
    decision = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("306001"),
    )

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"


# ---------------------------------------------------------------------------
# AUTO-APPROVE-EXPAND (§40차) — expanded-mode classification
# ---------------------------------------------------------------------------

# Cost rate deliberately 200bps so the "net P&L exactly zero" boundary lands on
# round numbers: net == 0 <=> limit * (1 - 0.02) == avg_buy_price.
_EXPANDED = AutoApproveLimits(
    min_distance_pct=Decimal("3"),
    per_order_cap=Decimal("200000"),
    daily_cap=Decimal("500000"),
    policy_version="test-policy",
    mode="expanded",
    breakeven_band_pct=Decimal("1"),
    round_trip_cost_bps=Decimal("200"),
)


def _evaluate(
    *, limits=_EXPANDED, group_overrides=None, preview=None, **rung_overrides
):
    return evaluate_auto_approve_eligibility(
        group=_group(**(group_overrides or {})),
        rung=_rung(**rung_overrides),
        preview=preview if preview is not None else {"success": True},
        limits=limits,
        daily_notional=Decimal("0"),
    )


def _sell_preview(*, current_price="100000", avg_buy_price="98000", **extra):
    preview = {
        "success": True,
        "current_price": current_price,
        "avg_buy_price": avg_buy_price,
    }
    preview.update(extra)
    return preview


def test_expanded_buy_inside_min_distance_is_eligible():
    """§40차 ① — a buy no longer has to rest 3% away, only below the market."""
    decision = _evaluate(
        limit_price=Decimal("99500"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert decision.eligible is True
    assert decision.details["mode"] == "expanded"


def test_off_mode_rejects_what_expanded_would_allow():
    """Mutant ⑧ — the §40차 rules must not leak into the default mode."""
    near_market_buy = {
        "limit_price": Decimal("99500"),
        "quantity": Decimal("1"),
        "preview": {"success": True, "current_price": "100000"},
    }
    profitable_near_market_sell = {
        "side": "sell",
        "limit_price": Decimal("100500"),
        "quantity": Decimal("1"),
        "preview": _sell_preview(),
    }

    for kwargs in (near_market_buy, profitable_near_market_sell):
        off = _evaluate(limits=_LIMITS, **kwargs)
        expanded = _evaluate(**kwargs)

        assert off.eligible is False
        assert off.reason == "distance_below_minimum"
        assert expanded.eligible is True


def test_expanded_marketable_orders_keep_the_veto_button_meaningful():
    buy = _evaluate(
        limit_price=Decimal("100001"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )
    sell = _evaluate(
        side="sell",
        limit_price=Decimal("99999"),
        quantity=Decimal("1"),
        preview=_sell_preview(),
    )

    assert (buy.eligible, buy.reason) == (False, "marketable_not_resting")
    assert (sell.eligible, sell.reason) == (False, "marketable_not_resting")


def test_expanded_limit_exactly_on_the_market_is_marketable():
    """A limit priced ON the market can fill before the card is seen."""
    buy = _evaluate(
        limit_price=Decimal("100000"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )
    sell = _evaluate(
        side="sell",
        limit_price=Decimal("100000"),
        quantity=Decimal("1"),
        preview=_sell_preview(),
    )

    assert (buy.eligible, buy.reason) == (False, "marketable_not_resting")
    assert (sell.eligible, sell.reason) == (False, "marketable_not_resting")


def test_off_mode_keeps_its_non_strict_distance_boundary():
    """ROB-871 boundary unchanged: exactly min_distance_pct away stays eligible."""
    buy = _evaluate(
        limits=_LIMITS,
        limit_price=Decimal("97000"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )
    sell = _evaluate(
        limits=_LIMITS,
        side="sell",
        limit_price=Decimal("103000"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert buy.eligible is True
    assert sell.eligible is True


def test_expanded_take_profit_sell_is_eligible():
    """§40차 ② — profit proven at the limit price, net of round-trip cost."""
    decision = _evaluate(
        side="sell",
        limit_price=Decimal("105000"),
        quantity=Decimal("1"),
        preview=_sell_preview(),
    )

    assert decision.eligible is True
    assert decision.details["loss_guard"] == "net_profit_proven"
    # gross 7000 - 105000 * 2% = 4900
    assert decision.details["gross_pnl"] == "7000"
    assert decision.details["round_trip_cost"] == "2100"
    assert decision.details["net_pnl"] == "4900"


def test_expanded_loss_cut_intent_never_auto_submits():
    """Mutant ① — the highest-priority leak."""
    decision = _evaluate(
        group_overrides={"exit_intent": "loss_cut"},
        side="sell",
        limit_price=Decimal("105000"),
        quantity=Decimal("1"),
        preview=_sell_preview(),
    )

    assert decision.eligible is False
    assert decision.reason == "loss_cut_intent"


def test_expanded_sell_below_cost_is_not_auto_approved():
    """Mutant ② — negative expected P&L, even with a passing preview."""
    decision = _evaluate(
        side="sell",
        limit_price=Decimal("105000"),
        quantity=Decimal("1"),
        preview=_sell_preview(avg_buy_price="110000"),
    )

    assert decision.eligible is False
    assert decision.reason == "expected_pnl_not_positive"


def test_expanded_net_pnl_exactly_zero_requires_approval():
    """Mutant ③ — "> 0" excludes 0. 100000 * (1 - 2%) == 98000."""
    at_zero = _evaluate(
        side="sell",
        limit_price=Decimal("100000"),
        quantity=Decimal("1"),
        preview=_sell_preview(current_price="99000"),
    )
    one_tick_above = _evaluate(
        side="sell",
        limit_price=Decimal("100001"),
        quantity=Decimal("1"),
        preview=_sell_preview(current_price="99000"),
    )

    assert at_zero.details["net_pnl"] == "0"
    assert at_zero.eligible is False
    assert at_zero.reason == "expected_pnl_not_positive"
    assert one_tick_above.eligible is True


def test_expanded_fee_rate_is_charged_before_the_sign_test():
    """A gross-positive sell that round-trip cost drags to <= 0 is not a profit."""
    gross_only = (Decimal("100000") - Decimal("98000")) * Decimal("1")
    decision = _evaluate(
        side="sell",
        limit_price=Decimal("100000"),
        quantity=Decimal("1"),
        preview=_sell_preview(current_price="99000"),
    )

    assert gross_only > 0
    assert Decimal(decision.details["round_trip_cost"]) >= gross_only
    assert decision.eligible is False


def test_expanded_trusts_the_more_pessimistic_preview_pnl():
    decision = _evaluate(
        side="sell",
        limit_price=Decimal("105000"),
        quantity=Decimal("1"),
        preview=_sell_preview(realized_pnl="1000"),
    )

    assert decision.details["gross_pnl"] == "1000"
    assert decision.eligible is False
    assert decision.reason == "expected_pnl_not_positive"


@pytest.mark.parametrize(
    ("limit_price", "current_price", "avg_buy_price"),
    [
        # +1% exactly above avg cost — the avg*1.01 guard's own boundary.
        (Decimal("101000"), Decimal("100000"), "100000"),
        # -1% exactly below avg cost, still resting above the market.
        (Decimal("99000"), Decimal("98000"), "100000"),
    ],
)
def test_expanded_breakeven_band_requires_approval(
    limit_price, current_price, avg_buy_price
):
    """Mutant ④ — ±1% around cost is a human's call whatever the P&L sign."""
    decision = _evaluate(
        side="sell",
        limit_price=limit_price,
        quantity=Decimal("1"),
        preview=_sell_preview(current_price=current_price, avg_buy_price=avg_buy_price),
    )

    assert decision.eligible is False
    assert decision.reason == "breakeven_band"


@pytest.mark.parametrize("mode_limits", [_LIMITS, _EXPANDED])
@pytest.mark.parametrize(
    "group_overrides",
    [
        {"rationale": {"tags": ["policy_deviation"]}},
        {"rationale": {"decision": {"flags": ["table_disagreement"]}}},
        {"exit_reason": "reviewed under Policy_Deviation waiver"},
        {"source_asof": {"table_disagreement": True}},
        {"lot_context": {"notes": ["table_disagreement"]}},
    ],
)
def test_tagged_proposals_require_approval_in_every_mode(mode_limits, group_overrides):
    """Mutant ⑤ — no pricing can buy a tagged proposal past the operator."""
    decision = _evaluate(
        limits=mode_limits,
        group_overrides=group_overrides,
        limit_price=Decimal("97000"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert decision.eligible is False
    assert decision.reason == "approval_required_tag"


def test_untagged_proposal_is_not_falsely_flagged():
    assert find_approval_required_tags(_group(thesis="ordinary support retest")) == ()


def test_repeated_dispatch_does_not_scan_its_own_rejection_audit():
    """Prior audit tokens stay evidence, never become new classifier input."""
    original = _group(
        rationale={"context": {"tags": ["policy_deviation"]}},
        thesis="ordinary support retest",
        strategy="ladder",
    )
    original_matches = find_approval_required_tag_matches(original)
    source_asof = append_auto_approve_rejection_attempt(
        {},
        decisions=[
            {
                "rung_index": 0,
                "eligible": False,
                "reason": "approval_required_tag",
                "policy_version": "2026-08-12.3",
                "tag_matches": original_matches,
            }
        ],
        now=datetime(2026, 8, 14, 0, 0, tzinfo=UTC),
    )

    audit_only = _group(source_asof=source_asof)
    repeated = _group(
        rationale=original.rationale,
        thesis="ordinary support retest",
        strategy="ladder",
        source_asof=source_asof,
    )

    assert find_approval_required_tags(audit_only) == ()
    assert find_approval_required_tags(repeated) == ("policy_deviation",)
    assert find_approval_required_tag_matches(repeated) == original_matches


def test_rejection_projection_drops_untrusted_metadata_without_losing_reason():
    raw_input = "private-metadata-must-not-escape"
    source_asof = {
        "auto_approve_rejections": [
            {
                "evaluated_at": "2026-08-14T00:00:00+00:00",
                "policy_version": raw_input,
                "rungs": [
                    {
                        "rung_index": 0,
                        "reason_code": "approval_required_tag",
                        "inputs": {
                            "policy_version": raw_input,
                            "tag_matches": [
                                {
                                    "token": "policy_deviation",
                                    "field": "rationale",
                                    "path": f"$.{raw_input}",
                                    "kind": "json_value",
                                    "char_start": 0,
                                }
                            ],
                            "error": raw_input,
                        },
                    }
                ],
            }
        ]
    }

    projected = project_auto_approve_rejections(source_asof)

    assert projected == [
        {
            "evaluated_at": "2026-08-14T00:00:00+00:00",
            "rungs": [
                {
                    "rung_index": 0,
                    "reason_code": "approval_required_tag",
                    "inputs": {},
                }
            ],
        }
    ]
    assert raw_input not in json.dumps(projected)
    malformed_time = dict(source_asof["auto_approve_rejections"][0])
    malformed_time["evaluated_at"] = raw_input
    assert (
        project_auto_approve_rejections({"auto_approve_rejections": [malformed_time]})
        == []
    )
    untrusted_reason = dict(source_asof["auto_approve_rejections"][0])
    untrusted_rung = dict(untrusted_reason["rungs"][0])
    untrusted_rung["reason_code"] = raw_input
    untrusted_reason["rungs"] = [untrusted_rung]
    assert (
        project_auto_approve_rejections(
            {"auto_approve_rejections": [untrusted_reason]}
        )[0]["rungs"][0]["reason_code"]
        == "invalid_reason_code"
    )


def test_cap_observation_projection_requires_complete_safe_fields():
    raw_input = "private-cap-observation-material-must-not-escape"
    valid = {
        "rung_index": 0,
        "daily_cap": "800",
        "daily_notional_before": "0",
        "daily_notional_after": "741.41",
        "per_order_cap": "1000",
        "notional": "741.41",
        "policy_version": "2026-08-14.1",
        "content_hash": "51c789434f6a",
        "evaluated_at": "2026-08-14T22:35:00+00:00",
        "private": raw_input,
    }
    incomplete = dict(valid)
    incomplete.pop("daily_notional_after")

    projected = project_auto_approve_cap_observations(
        {"auto_approved": {"cap_observations": [valid, incomplete]}}
    )

    assert projected == [
        {
            "rung_index": 0,
            "daily_cap": "800",
            "daily_notional_before": "0",
            "daily_notional_after": "741.41",
            "per_order_cap": "1000",
            "notional": "741.41",
            "policy_version": "2026-08-14.1",
            "content_hash": "51c789434f6a",
            "evaluated_at": "2026-08-14T22:35:00+00:00",
        }
    ]
    assert raw_input not in json.dumps(projected)


def test_cap_observation_stamp_is_decision_inert_across_26000_fuzz_cases():
    randomizer = random.Random(1244)
    unobserved_limits = replace(_EXPANDED, policy_content_hash=None)
    observed_limits = replace(
        _EXPANDED,
        policy_content_hash="51c789434f6a",
    )
    observation = {
        "rung_index": 0,
        "daily_cap": "800",
        "daily_notional_before": "0",
        "daily_notional_after": "741.41",
        "per_order_cap": "1000",
        "notional": "741.41",
        "policy_version": "2026-08-14.1",
        "content_hash": "51c789434f6a",
        "evaluated_at": "2026-08-14T22:35:00+00:00",
    }
    compared = 0
    mismatches: list[int] = []
    for case in range(26_000):
        group_kwargs = {
            "market": randomizer.choice(["equity_kr", "equity_us", "crypto", "index"]),
            "account_mode": randomizer.choice(
                ["kis_live", "toss_live", "upbit", "unknown"]
            ),
            "order_type": randomizer.choice(["limit", "market", "unknown"]),
            "action": randomizer.choice(["place", "replace", "cancel", None]),
            "exit_intent": randomizer.choice([None, "loss_cut", "other_exit"]),
            "thesis": randomizer.choice(["cap fixture", "", None]),
        }
        rung = _rung(
            side=randomizer.choice(["buy", "sell", "other"]),
            limit_price=randomizer.choice(
                [
                    Decimal("0"),
                    Decimal("741.41"),
                    Decimal("800"),
                    Decimal("1000"),
                ]
            ),
            quantity=randomizer.choice(
                [Decimal("0"), Decimal("1"), Decimal("2"), Decimal("3")]
            ),
        )
        preview = {
            "success": randomizer.choice([True, False, "true", None]),
            "current_price": randomizer.choice(
                [None, "0", "741.41", "800", "not-a-price"]
            ),
            "avg_buy_price": randomizer.choice(
                [None, "700", "741.41", "800", "not-a-price"]
            ),
            "realized_pnl": randomizer.choice([None, "-1", "0", "1", "not-a-price"]),
        }
        daily_notional = Decimal(randomizer.randrange(0, 120_001)) / Decimal("100")
        baseline = evaluate_auto_approve_eligibility(
            group=_group(**group_kwargs, source_asof={}),
            rung=rung,
            preview=preview,
            limits=unobserved_limits,
            daily_notional=daily_notional,
        )
        observed = evaluate_auto_approve_eligibility(
            group=_group(
                **group_kwargs,
                source_asof={"auto_approved": {"cap_observations": [observation]}},
            ),
            rung=rung,
            preview=preview,
            limits=observed_limits,
            daily_notional=daily_notional,
        )
        compared += 1
        if observed != baseline:
            mismatches.append(case)

    print(
        f"decision_differential_cases={compared} decision_mismatches={len(mismatches)}"
    )
    assert compared == 26_000
    assert mismatches == []


def test_tag_match_evidence_records_token_and_structural_location_only():
    group = _group(
        rationale={"context": {"tags": ["policy_deviation"]}},
        thesis="ordinary support retest",
        strategy="ladder",
    )

    decision = evaluate_auto_approve_eligibility(
        group=group,
        rung=_rung(limit_price=Decimal("97000"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "100000"},
        limits=_LIMITS,
        daily_notional=Decimal("0"),
    )

    expected = [
        {
            "token": "policy_deviation",
            "field": "rationale",
            "path": "$.context.tags[0]",
            "kind": "json_value",
            "char_start": 0,
        }
    ]
    assert decision.reason == "approval_required_tag"
    assert decision.details["tag_matches"] == expected
    assert find_approval_required_tag_matches(group) == expected


def test_unserializable_metadata_is_treated_as_tagged():
    class _Hostile:
        def __getattr__(self, name):
            raise RuntimeError("metadata unavailable")

    assert find_approval_required_tags(_Hostile()) == (
        "policy_deviation",
        "table_disagreement",
    )


@pytest.mark.parametrize(
    ("preview_overrides", "expected_reason"),
    [
        ({"avg_buy_price": None}, "sell_classification_unavailable"),
        ({"avg_buy_price": "0"}, "sell_classification_unavailable"),
        ({"avg_buy_price": "not-a-number"}, "sell_classification_unavailable"),
    ],
)
def test_expanded_unclassifiable_sell_fails_closed(preview_overrides, expected_reason):
    """Mutant ⑥ — an unknown cost basis is not a clearance."""
    preview = _sell_preview()
    preview.update(preview_overrides)
    decision = _evaluate(
        side="sell",
        limit_price=Decimal("105000"),
        quantity=Decimal("1"),
        preview=preview,
    )

    assert decision.eligible is False
    assert decision.reason == expected_reason


def test_expanded_does_not_widen_the_eligible_account_set():
    """Mutant ⑦ — §40차 must not hand auto-approval to a non-cancellable lane."""
    for account_mode, market in (
        ("toss_live", "equity_kr"),
        ("kis_mock", "equity_kr"),
        ("kiwoom_mock", "equity_kr"),
        ("alpaca_paper", "equity_us"),
    ):
        decision = _evaluate(
            group_overrides={"account_mode": account_mode, "market": market},
            limit_price=Decimal("99500"),
            quantity=Decimal("1"),
            preview={"success": True, "current_price": "100000"},
        )

        assert decision.eligible is False
        assert decision.reason == "account_not_veto_capable"


def test_unknown_mode_fails_closed():
    decision = _evaluate(
        limits=AutoApproveLimits(
            min_distance_pct=Decimal("3"),
            per_order_cap=Decimal("200000"),
            daily_cap=Decimal("500000"),
            policy_version="test-policy",
            mode="on",
        ),
        limit_price=Decimal("97000"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert decision.eligible is False
    assert decision.reason == "unknown_auto_approve_mode"


def test_expanded_still_honours_the_existing_caps():
    per_order = _evaluate(
        limit_price=Decimal("99000"),
        quantity=Decimal("3"),
        preview={"success": True, "current_price": "100000"},
    )
    daily = evaluate_auto_approve_eligibility(
        group=_group(),
        rung=_rung(limit_price=Decimal("99000"), quantity=Decimal("1")),
        preview={"success": True, "current_price": "100000"},
        limits=_EXPANDED,
        daily_notional=Decimal("401001"),
    )

    assert per_order.reason == "per_order_cap_exceeded"
    assert daily.reason == "daily_cap_exceeded"


def test_shipped_default_mode_is_off(monkeypatch):
    """ENV_GATE — the repo ships the expansion inert."""
    from app.core.config import Settings

    assert Settings.model_fields["ORDER_PROPOSALS_AUTO_APPROVE_MODE"].default == "off"
    assert (
        Settings.model_fields["ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED"].default is False
    )
    assert limits_for_market("equity_kr").mode == "off"


def test_toss_live_requires_its_separate_default_off_veto_gate(monkeypatch):
    from app.services.order_proposals import auto_approve as module

    blocked = _evaluate(
        group_overrides={"account_mode": "toss_live", "market": "equity_kr"},
        limit_price=Decimal("99500"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )
    monkeypatch.setattr(module.settings, "ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED", True)
    allowed = _evaluate(
        group_overrides={"account_mode": "toss_live", "market": "equity_kr"},
        limit_price=Decimal("99500"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert (blocked.eligible, blocked.reason) == (False, "account_not_veto_capable")
    assert (allowed.eligible, allowed.reason) == (True, "eligible")


def test_auto_veto_card_requires_a_thesis_not_a_strategy_fallback():
    decision = _evaluate(
        group_overrides={"thesis": "", "strategy": "mean_reversion"},
        limit_price=Decimal("99500"),
        quantity=Decimal("1"),
        preview={"success": True, "current_price": "100000"},
    )

    assert (decision.eligible, decision.reason) == (
        False,
        "thesis_required_for_veto_card",
    )


def test_auto_veto_card_has_symbol_quantity_price_and_thesis_fields():
    group = _group(
        proposal_id=uuid.uuid4(),
        symbol="005930",
        side="buy",
        thesis="valuation dislocation",
    )
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce="fixture-nonce",
        attempt_id=uuid.uuid4(),
        card_kind=ApprovalCardKind.AUTO_VETO,
        current_membership_revision=None,
    )

    text, _keyboard = build_auto_approved_message(
        group=group,
        rungs=[_rung(quantity=Decimal("2"), limit_price=Decimal("97000"))],
        nonce="fixture-nonce",
        policy_version="fixture-policy",
        binding=binding,
    )

    assert "종목: `005930`" in text
    assert "수량: #1 2" in text
    assert "가격: #1 97000" in text
    assert "근거: valuation dislocation" in text


def test_auto_veto_card_raises_when_thesis_is_missing():
    group = _group(
        proposal_id=uuid.uuid4(),
        symbol="005930",
        side="buy",
        thesis=" ",
    )
    binding = build_proposal_dispatch_binding(
        proposal_id=group.proposal_id,
        nonce="fixture-nonce",
        attempt_id=uuid.uuid4(),
        card_kind=ApprovalCardKind.AUTO_VETO,
        current_membership_revision=None,
    )

    with pytest.raises(ValueError, match="non-empty thesis"):
        build_auto_approved_message(
            group=group,
            rungs=[_rung()],
            nonce="fixture-nonce",
            policy_version="fixture-policy",
            binding=binding,
        )


def test_policy_caps_follow_the_declared_upper_bands_and_one_new_entry_limit():
    kr = limits_for_market("equity_kr")
    us = limits_for_market("equity_us")

    assert kr is not None
    assert us is not None
    # policy buy.per_symbol_notional_krw_range=[200000,400000],
    # buy.per_symbol_notional_usd_range=[150,450], and one concurrent new
    # entry; see the ownership-minimal derivation comment in the cap block.
    assert (kr.per_order_cap, kr.daily_cap) == (Decimal("400000"), Decimal("400000"))
    # §65차/§71차 (2026-08-14): per-order 800 admits single-share exits of
    # $500-750 ETFs; daily 5000 covers a full observed trim session instead of
    # being consumed by its first rung. The sizing band above is unchanged.
    assert (us.per_order_cap, us.daily_cap) == (Decimal("800"), Decimal("5000"))


def test_policy_cost_rate_cannot_be_edited_below_the_code_floor(monkeypatch):
    """FEE_RATE — a cheaper YAML must not widen the profit-take test."""
    from app.services.order_proposals import auto_approve as module

    cheap = SimpleNamespace(
        version="probe",
        order_proposals=SimpleNamespace(
            auto_approve=SimpleNamespace(
                min_distance_pct=3,
                per_order_cap={"kr": 200000, "us": 150, "crypto": 100000},
                daily_cap={"kr": 500000, "us": 400, "crypto": 300000},
                breakeven_band_pct=1,
                round_trip_cost_bps={"kr": 0, "us": 0, "crypto": 0},
            )
        ),
    )
    monkeypatch.setattr(module, "load_trading_policy", lambda: cheap)

    assert limits_for_market("equity_kr").round_trip_cost_bps == Decimal("47.4")
    assert limits_for_market("equity_us").round_trip_cost_bps == Decimal("90")
    assert limits_for_market("crypto").round_trip_cost_bps == Decimal("10")


def test_expanded_mode_flows_from_settings(monkeypatch):
    from app.services.order_proposals import auto_approve as module

    monkeypatch.setattr(
        module.settings, "ORDER_PROPOSALS_AUTO_APPROVE_MODE", "expanded"
    )

    assert limits_for_market("equity_kr").mode == "expanded"


@pytest.mark.asyncio
async def test_daily_notional_uses_auto_approval_time_not_create_time(db_session):
    service = OrderProposalsService(db_session)
    now = datetime.now(UTC)
    account_id = f"daily-{uuid.uuid4()}"

    for approved_at in (now, now - timedelta(days=1)):
        await service.create_proposal(
            symbol="005930",
            market="equity_kr",
            account_mode="kis_live",
            broker_account_id=account_id,
            side="buy",
            order_type="limit",
            proposer="p",
            rungs=[RungInput(0, "buy", Decimal("1"), Decimal("200000"), None)],
            source_asof={
                "auto_approved": {
                    "policy_version": "test-policy",
                    "approved_at": approved_at.isoformat(),
                    "eligibility": [],
                    "outcomes": ["submitted_resting"],
                }
            },
        )
    await service.create_proposal(
        symbol="AAPL",
        market="equity_us",
        account_mode="kis_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("100"), None)],
        source_asof={
            "auto_approved": {
                "policy_version": "test-policy",
                "approved_at": now.isoformat(),
                "eligibility": [],
                "outcomes": ["submitted_resting"],
            }
        },
    )
    await db_session.commit()
    probe = await service.create_proposal(
        symbol="000660",
        market="equity_kr",
        account_mode="kis_live",
        broker_account_id=account_id,
        side="buy",
        order_type="limit",
        proposer="p",
        rungs=[RungInput(0, "buy", Decimal("1"), Decimal("1"), None)],
    )

    total = await service.auto_approved_daily_notional(probe, now=now)

    assert total == Decimal("200000")
