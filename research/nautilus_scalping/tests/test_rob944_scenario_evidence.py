"""ROB-944 (H4, ROB-940) — H4-owned scenario terminal-evidence contract tests.

Captain audit supplement (2026-07-17): ``rob940_engine.ledger_hash`` hashes
ONLY the trade ledger -- it must never stand in as the full scenario
artifact hash, because two runs with equally-empty trade ledgers but
DIFFERENT no-trade reasons must still hash differently. This module's
``scenario_artifact_hash`` binds identity + status + trades + the full
no-trade reason histogram.
"""

from __future__ import annotations

import pytest
from rob940_engine import EngineResult, NoTradeRecord, TradeRecord
from rob944_scenario_evidence import (
    ScenarioRunOutcome,
    no_trade_reason_counts,
    scenario_artifact_hash,
    scenario_run_outcome_from_engine_result,
)

_IDENTITY = {
    "strategy": "S1",
    "config_id": "S1-00",
    "symbol": "BTCUSDT",
    "fold_id": "fold-00",
    "scenario_name": "primary_stress",
}


def _no_trade(reason, signal_ts=1):
    return NoTradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=signal_ts,
        reason=reason,
    )


def _trade(net_bps=10.0, entry_ts=1000, exit_ts=2000):
    return TradeRecord(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=entry_ts,
        entry_ts=entry_ts,
        entry_price=100.0,
        exit_ts=exit_ts,
        exit_price=101.0,
        exit_reason="take_profit",
        gross_bps=100.0,
        fee_bps=10.0,
        all_in_bps=17.0,
        funding_bps=0.0,
        net_bps=net_bps,
        fold_id="fold-00",
    )


def test_two_empty_ledgers_with_different_no_trade_reasons_hash_differently():
    result_a = EngineResult(trades=(), no_trades=(_no_trade("daily_stop_active"),))
    result_b = EngineResult(trades=(), no_trades=(_no_trade("tp_below_min_distance"),))
    hash_a = scenario_artifact_hash(result_a, **_IDENTITY)
    hash_b = scenario_artifact_hash(result_b, **_IDENTITY)
    assert hash_a != hash_b


def test_two_empty_ledgers_with_identical_no_trade_reasons_hash_identically():
    result_a = EngineResult(trades=(), no_trades=(_no_trade("daily_stop_active", 1),))
    result_b = EngineResult(trades=(), no_trades=(_no_trade("daily_stop_active", 2),))
    # Different signal_ts but same reason histogram shape is intentionally
    # NOT distinguished by this identity-level hash (it summarizes reason
    # counts, not full no-trade record contents) -- documented behavior.
    hash_a = scenario_artifact_hash(result_a, **_IDENTITY)
    hash_b = scenario_artifact_hash(result_b, **_IDENTITY)
    assert hash_a == hash_b


def test_hash_changes_when_trade_ledger_differs():
    result_a = EngineResult(trades=(_trade(net_bps=10.0),), no_trades=())
    result_b = EngineResult(trades=(_trade(net_bps=20.0),), no_trades=())
    assert scenario_artifact_hash(result_a, **_IDENTITY) != scenario_artifact_hash(
        result_b, **_IDENTITY
    )


def test_hash_changes_when_scenario_identity_differs():
    result = EngineResult(trades=(), no_trades=())
    base_hash = scenario_artifact_hash(result, **_IDENTITY)
    other = dict(_IDENTITY, scenario_name="base")
    assert scenario_artifact_hash(result, **other) != base_hash


def test_hash_is_not_h2s_bare_ledger_hash():
    from rob940_engine import ledger_hash

    result = EngineResult(trades=(), no_trades=(_no_trade("daily_stop_active"),))
    assert scenario_artifact_hash(result, **_IDENTITY) != ledger_hash(result.trades)


def test_scenario_run_outcome_from_engine_result_is_completed_with_required_fields():
    result = EngineResult(trades=(_trade(),), no_trades=())
    outcome = scenario_run_outcome_from_engine_result(result, **_IDENTITY)
    assert outcome.status == "completed"
    assert outcome.trade_count == 1
    assert isinstance(outcome.artifact_hash, str) and len(outcome.artifact_hash) == 64
    assert outcome.error_reason is None


def test_scenario_run_outcome_rejects_unknown_scenario_name():
    with pytest.raises(ValueError):
        ScenarioRunOutcome(
            scenario_name="not_a_real_scenario",
            status="completed",
            trade_count=0,
            artifact_hash="a" * 64,
        )


def test_scenario_run_outcome_rejects_negative_trade_count():
    with pytest.raises(ValueError):
        ScenarioRunOutcome(
            scenario_name="base",
            status="completed",
            trade_count=-1,
            artifact_hash="a" * 64,
        )


def test_scenario_run_outcome_requires_error_reason_for_non_completed_status():
    with pytest.raises(ValueError):
        ScenarioRunOutcome(
            scenario_name="base",
            status="crashed",
            trade_count=0,
            artifact_hash="a" * 64,
            error_reason=None,
        )
    # With a reason, it is accepted.
    outcome = ScenarioRunOutcome(
        scenario_name="base",
        status="crashed",
        trade_count=0,
        artifact_hash="a" * 64,
        error_reason="engine raised ValueError",
    )
    assert outcome.status == "crashed"


def test_scenario_run_outcome_accepts_rejected_status_with_reason():
    outcome = ScenarioRunOutcome(
        scenario_name="base",
        status="rejected",
        trade_count=0,
        artifact_hash="a" * 64,
        error_reason="rejected:data_gap_in_position",
    )
    assert outcome.status == "rejected"


# ---------------------------------------------------------------------------
# Captain Q3-enforcement correction: no_trade_reason_counts is REPORT
# EXPOSURE, not merely hash-committed -- funding_evidence_unavailable and
# expected_funding_cost_above_3bps (and every other no-trade reason) must
# remain separately countable, not hidden behind a SHA-256 digest.
# ---------------------------------------------------------------------------


def test_no_trade_reason_counts_exposes_the_same_histogram_the_hash_commits_to():
    result = EngineResult(
        trades=(),
        no_trades=(
            _no_trade("funding_evidence_unavailable", 1),
            _no_trade("funding_evidence_unavailable", 2),
            _no_trade("expected_funding_cost_above_3bps", 3),
        ),
    )
    counts = no_trade_reason_counts(result)
    assert counts == {
        "funding_evidence_unavailable": 2,
        "expected_funding_cost_above_3bps": 1,
    }


def test_scenario_run_outcome_from_engine_result_exposes_no_trade_reason_counts():
    result = EngineResult(
        trades=(_trade(),),
        no_trades=(
            _no_trade("funding_evidence_unavailable", 1),
            _no_trade("expected_funding_cost_above_3bps", 2),
        ),
    )
    outcome = scenario_run_outcome_from_engine_result(result, **_IDENTITY)
    assert outcome.no_trade_reason_counts == {
        "funding_evidence_unavailable": 1,
        "expected_funding_cost_above_3bps": 1,
    }
    # Both distinct funding reasons remain separately countable -- neither
    # collapses into the other nor disappears behind the artifact hash.
    assert outcome.no_trade_reason_counts["funding_evidence_unavailable"] == 1
    assert outcome.no_trade_reason_counts["expected_funding_cost_above_3bps"] == 1


def test_scenario_run_outcome_default_no_trade_reason_counts_is_empty_dict():
    outcome = ScenarioRunOutcome(
        scenario_name="base",
        status="crashed",
        trade_count=0,
        artifact_hash="a" * 64,
        error_reason="child_execution_crashed",
    )
    assert outcome.no_trade_reason_counts == {}
