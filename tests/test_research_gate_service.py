import json

from app.services.research_gate_service import evaluate_candidate


def test_reject_when_closed_trade_count_is_too_low() -> None:
    result = evaluate_candidate(
        total_trades=8,
        profit_factor=1.8,
        max_drawdown=0.08,
        config={"minimum_trade_count": 20},
    )

    assert result.status == "FAIL"
    assert result.reason_code == "MIN_TRADES"


def test_reject_when_profit_factor_below_threshold() -> None:
    result = evaluate_candidate(
        total_trades=30,
        profit_factor=1.05,
        max_drawdown=0.08,
        config={
            "minimum_trade_count": 20,
            "minimum_profit_factor": 1.2,
        },
    )

    assert result.status == "FAIL"
    assert result.reason_code == "LOW_PROFIT_FACTOR"


def test_pass_when_all_thresholds_are_satisfied() -> None:
    result = evaluate_candidate(
        total_trades=30,
        profit_factor=1.6,
        max_drawdown=0.08,
        config={
            "minimum_trade_count": 20,
            "minimum_profit_factor": 1.2,
            "maximum_drawdown": 0.2,
        },
    )

    assert result.status == "PASS"
    assert result.reason_code == "OK"


def test_thresholds_are_json_safe_when_optional_limits_missing() -> None:
    result = evaluate_candidate(
        total_trades=30,
        profit_factor=1.3,
        max_drawdown=0.08,
        config={"minimum_trade_count": 20},
    )

    # Thresholds are persisted to JSONB and must not contain NaN/Infinity values.
    serialized = json.dumps(result.thresholds, allow_nan=False)
    assert serialized
