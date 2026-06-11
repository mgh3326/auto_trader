"""ROB-477: pure fill-safety analysis for sell limit ladders."""

import pytest

from app.services.orders.ladder_fill_safety import (
    LadderRung,
    evaluate_ladder_fill_safety,
)


def test_all_above_and_no_near_anchor_fires_both_warnings():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[
            LadderRung(limit_price=66.0, quantity=2.0),
            LadderRung(limit_price=68.0, quantity=3.0),
        ],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" in warnings
    assert details["allRungsAboveMarket"] is True
    assert details["hasNearMarketAnchor"] is False


def test_all_above_but_lowest_rung_within_threshold_fires_only_all_above():
    # IONQ incident shape: 64.00 vs anchor 63.95 = +0.078% < 0.3% threshold.
    # Near-above rung IS a near-market anchor -> second warning must be absent.
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[
            LadderRung(limit_price=64.0, quantity=2.0),
            LadderRung(limit_price=66.0, quantity=3.0),
        ],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["hasNearMarketAnchor"] is True
    assert details["rungs"][0]["nearAboveMarket"] is True


def test_marketable_rung_clears_both_warnings():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[
            LadderRung(limit_price=63.95, quantity=2.0),
            LadderRung(limit_price=66.0, quantity=3.0),
        ],
        anchor_price=63.95,
    )
    assert warnings == []
    assert details["allRungsAboveMarket"] is False
    assert details["hasMarketableAnchor"] is True


def test_atr_widens_near_threshold():
    # pct threshold = 63.95*0.3% ~= 0.1919; ATR 4.0 * 0.3 = 1.2 -> 64.9 is near.
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[
            LadderRung(limit_price=64.9, quantity=1.0),
            LadderRung(limit_price=68.0, quantity=1.0),
        ],
        anchor_price=63.95,
        atr=4.0,
    )
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["nearMarketThresholdUsd"] == pytest.approx(1.2)
    assert details["rungs"][1]["atrMultiple"] == pytest.approx(1.0125, abs=1e-4)


def test_single_rung_above_market_still_warns():
    warnings, _ = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=70.0)],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings


def test_empty_rungs_or_bad_anchor_returns_no_analysis():
    assert evaluate_ladder_fill_safety(rungs=[], anchor_price=63.95) == ([], None)
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.0)], anchor_price=None
    ) == ([], None)
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=64.0)], anchor_price=0.0
    ) == ([], None)


def test_non_positive_rung_is_invalid_and_never_satisfies_anchor():
    # A garbage 0.0 rung must NOT suppress the warnings (review P3).
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[
            LadderRung(limit_price=0.0, quantity=1.0),
            LadderRung(limit_price=66.0, quantity=2.0),
            LadderRung(limit_price=68.0, quantity=3.0),
        ],
        anchor_price=63.95,
    )
    assert "ladder_all_above_market" in warnings
    assert "ladder_missing_near_market_anchor" in warnings
    assert details["invalidRungCount"] == 1
    assert details["rungs"][0]["invalid"] is True


def test_all_rungs_invalid_returns_no_analysis():
    assert evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=0.0), LadderRung(limit_price=-1.0)],
        anchor_price=63.95,
    ) == ([], None)


def test_suggested_anchor_rung_present_only_when_warning():
    warnings, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=66.0), LadderRung(limit_price=68.0)],
        anchor_price=63.95,
    )
    assert details["suggestedAnchorRung"]["limitPriceUsd"] == 63.95
    clean_warnings, clean_details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=63.0), LadderRung(limit_price=66.0)],
        anchor_price=63.95,
    )
    assert clean_warnings == []
    assert "suggestedAnchorRung" not in clean_details


# ---------------------------------------------------------------------------
# ROB-507: buy-side mirror (side="buy")
# ---------------------------------------------------------------------------
def test_buy_all_below_and_no_near_anchor_fires_both_warnings():
    # GLW-shaped entry ladder: every rung well below the anchor -> zero-fill
    # tail risk in a rally (mirror of the 2026-06-09 sell incident).
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[
            LadderRung(limit_price=165.5, quantity=10.0),
            LadderRung(limit_price=157.25, quantity=10.0),
        ],
        anchor_price=168.9,
    )
    assert "ladder_all_below_market" in warnings
    assert "ladder_missing_near_market_anchor" in warnings
    assert details["allRungsBelowMarket"] is True
    assert details["hasNearMarketAnchor"] is False


def test_buy_highest_rung_within_threshold_fires_only_all_below():
    # 168.5 vs anchor 168.9 = -0.237% within the 0.3% threshold.
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[
            LadderRung(limit_price=168.5, quantity=10.0),
            LadderRung(limit_price=157.25, quantity=10.0),
        ],
        anchor_price=168.9,
    )
    assert "ladder_all_below_market" in warnings
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["hasNearMarketAnchor"] is True
    assert details["rungs"][0]["nearBelowMarket"] is True


def test_buy_marketable_rung_clears_both_warnings():
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[
            LadderRung(limit_price=168.9, quantity=10.0),
            LadderRung(limit_price=157.25, quantity=10.0),
        ],
        anchor_price=168.9,
    )
    assert warnings == []
    assert details["allRungsBelowMarket"] is False
    assert details["hasMarketableAnchor"] is True


def test_buy_atr_widens_near_threshold():
    # pct threshold = 168.9*0.3% ~= 0.5067; ATR 4.0 * 0.3 = 1.2 -> 167.8 is near.
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[
            LadderRung(limit_price=167.8, quantity=1.0),
            LadderRung(limit_price=157.25, quantity=1.0),
        ],
        anchor_price=168.9,
        atr=4.0,
    )
    assert "ladder_missing_near_market_anchor" not in warnings
    assert details["rungs"][0]["nearBelowMarket"] is True


def test_buy_distance_sign_and_atr_multiple_are_risk_oriented():
    # For buys the risk distance is anchor - rung (positive = away from market,
    # below), mirroring the sell convention (positive = above market).
    _, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[LadderRung(limit_price=160.0, quantity=1.0)],
        anchor_price=170.0,
        atr=5.0,
    )
    rung = details["rungs"][0]
    assert rung["distanceUsd"] == 10.0
    assert rung["distancePct"] == pytest.approx(10.0 / 170.0 * 100.0, abs=1e-3)
    assert rung["atrMultiple"] == 2.0
    assert rung["belowMarket"] is True
    assert rung["marketableAnchor"] is False


def test_buy_suggested_anchor_rung_mentions_buy():
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[LadderRung(limit_price=150.0, quantity=1.0)],
        anchor_price=170.0,
    )
    assert warnings
    assert "buy" in details["suggestedAnchorRung"]["rationale"]


def test_buy_invalid_rungs_excluded_like_sell():
    warnings, details = evaluate_ladder_fill_safety(
        side="buy",
        rungs=[
            LadderRung(limit_price=0.0, quantity=1.0),
            LadderRung(limit_price=160.0, quantity=1.0),
        ],
        anchor_price=170.0,
    )
    assert details["invalidRungCount"] == 1
    assert "ladder_all_below_market" in warnings


def test_invalid_side_raises():
    with pytest.raises(ValueError):
        evaluate_ladder_fill_safety(
            side="hold",
            rungs=[LadderRung(limit_price=160.0)],
            anchor_price=170.0,
        )


def test_sell_default_side_output_keys_unchanged():
    # Byte-compat guard: omitting side keeps the exact sell key set.
    _, details = evaluate_ladder_fill_safety(
        rungs=[LadderRung(limit_price=66.0, quantity=2.0)],
        anchor_price=63.95,
    )
    assert "allRungsAboveMarket" in details
    assert "allRungsBelowMarket" not in details
    assert "aboveMarket" in details["rungs"][0]
    assert "belowMarket" not in details["rungs"][0]
