"""ROB-116 — Pure-function classifier tests."""

import pytest

from app.services.portfolio_action_classifier import (
    ClassifierInputs,
    classify_position,
)


def _inputs(**overrides) -> ClassifierInputs:
    base = ClassifierInputs(
        symbol="KRW-SOL",
        position_weight_pct=10.0,
        profit_rate=2.0,
        summary_decision=None,
        summary_confidence=None,
        market_verdict=None,
        nearest_support_pct=None,
        nearest_resistance_pct=None,
        journal_status="missing",
        sellable_quantity=None,
        staked_quantity=None,
    )
    return base.model_copy(update=overrides)


@pytest.mark.unit
def test_overweight_with_bearish_research_classifies_as_trim() -> None:
    result = classify_position(
        _inputs(position_weight_pct=29.75, summary_decision="hold")
    )
    assert result.candidate_action == "trim"
    assert result.suggested_trim_pct == 20
    assert "overweight" in result.reason_codes
    assert "research_not_bullish" in result.reason_codes


@pytest.mark.unit
def test_buy_decision_under_weight_target_classifies_as_add() -> None:
    result = classify_position(
        _inputs(position_weight_pct=2.0, summary_decision="buy", summary_confidence=72)
    )
    assert result.candidate_action == "add"
    assert "research_bullish" in result.reason_codes


@pytest.mark.unit
def test_sell_decision_with_loss_classifies_as_sell() -> None:
    result = classify_position(
        _inputs(profit_rate=-15.0, summary_decision="sell", summary_confidence=70)
    )
    assert result.candidate_action == "sell"
    assert "research_bearish" in result.reason_codes


@pytest.mark.unit
def test_no_research_summary_classifies_as_watch() -> None:
    result = classify_position(_inputs(summary_decision=None))
    assert result.candidate_action == "watch"
    assert "research_missing" in result.reason_codes


@pytest.mark.unit
def test_journal_missing_emits_missing_context() -> None:
    result = classify_position(_inputs(summary_decision="hold"))
    assert "journal_missing" in result.missing_context_codes


@pytest.mark.unit
def test_near_resistance_adds_reason_code() -> None:
    result = classify_position(
        _inputs(
            position_weight_pct=20.0,
            summary_decision="hold",
            nearest_resistance_pct=0.8,
        )
    )
    assert "near_resistance" in result.reason_codes
