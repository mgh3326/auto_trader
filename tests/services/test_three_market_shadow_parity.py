from __future__ import annotations

from app.services.kis_lean_execution import SyntheticNoOpStrategy
from app.services.three_market_shadow import shadow_decision
from app.services.three_market_shadow_lifecycle import verify_crypto_acceptance_path
from research.three_market_shadow.calculations import (
    CONTRACT_HASH,
    calculate_signal,
)
from research.three_market_shadow.harness import run_harness


def _snapshot() -> dict[str, object]:
    closes = [100.0 + index for index in range(25)]
    volumes = [100.0] * 25
    return {"symbol": "005930", "close": closes, "volume": volumes}


def test_runner_and_harness_import_and_return_same_pure_calculation() -> None:
    snapshot = _snapshot()
    runner_signal = SyntheticNoOpStrategy().evaluate(snapshot)
    harness_signal = run_harness("kr", snapshot)["signal"]

    assert runner_signal["contract_hash"] == CONTRACT_HASH
    assert {key: value for key, value in runner_signal.items() if key != "labels"} == {
        "decision": "NO_ORDER",
        "symbol": "005930",
        **harness_signal,
    }
    assert run_harness("kr", snapshot)["contract_hash"] == CONTRACT_HASH


def test_dispatch_is_shared_for_kr_us_and_crypto() -> None:
    snapshot = _snapshot()
    for market in ("kr", "us", "crypto"):
        assert run_harness(market, snapshot)["signal"] == calculate_signal(
            market, snapshot
        )


def test_shadow_has_acceptance_labels_and_zero_mutation() -> None:
    result = shadow_decision("us", _snapshot())
    assert result["order_count"] == 0
    assert result["account_mutations"] == 0
    assert result["arm"] is False
    assert result["labels"] == {
        "purpose": "execution_acceptance",
        "sample_class": "ACCEPTANCE_ONLY",
        "signal_source": "UNTESTED_RESEARCH_SHADOW",
        "evidence_preserved": "YES",
        "scoring_eligible": "EXCLUDE",
        "forecast_calibration_eligible": "EXCLUDE",
        "trade_performance_eligible": "EXCLUDE",
        "strategy_promotion_credit": "NONE",
        "paper_go_live_credit": "NONE",
        "operational_reliability_eligible": "UNIDENTIFIABLE",
        "completion_name": "BROKER_ACCEPTANCE_PASSED",
    }


def test_crypto_path_is_signal_intent_block_kill_restart() -> None:
    path = verify_crypto_acceptance_path()
    assert [item.get("kind") for item in path[1:]] == [
        "order_intent",
        "pre_submit_block",
        "kill",
        "restart",
    ]
    assert path[0]["signal_source"] == "SYNTHETIC_ACCEPTANCE"
    assert path[2]["accepted_for_submission"] is False
    assert path[-1]["orders"] == 0
