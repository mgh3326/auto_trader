from __future__ import annotations

import random

import output_schema as os_
import pytest


def _record(
    *, decision_ts_ms=1, strategy="AP-A1", config_id="AP-A1-00", symbol="AAA/USD"
):
    return os_.SignalRecord(
        decision_ts_ms=decision_ts_ms,
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        action="ENTER",
        target_notional=100.0,
        reason_code="ENTRY_ACCEPTED",
        evidence_hash="deadbeef",
    )


def test_action_must_match_the_reason_codes_mapped_action():
    with pytest.raises(os_.ActionReasonMismatchError, match="NO_ENTRY_SIGNAL"):
        os_.SignalRecord(
            decision_ts_ms=1,
            strategy="AP-A1",
            config_id="AP-A1-00",
            symbol="AAA/USD",
            action="ENTER",  # wrong -- NO_ENTRY_SIGNAL maps to NO_ACTION
            target_notional=0.0,
            reason_code="NO_ENTRY_SIGNAL",
            evidence_hash="x",
        )


def test_non_enter_record_must_carry_zero_target_notional():
    with pytest.raises(ValueError, match="target_notional"):
        os_.SignalRecord(
            decision_ts_ms=1,
            strategy="AP-A1",
            config_id="AP-A1-00",
            symbol="AAA/USD",
            action="NO_ACTION",
            target_notional=50.0,  # leaked notional on a rejected candidate
            reason_code="MIN_TARGET_NOTIONAL",
            evidence_hash="x",
        )


def test_unknown_strategy_is_rejected():
    with pytest.raises(ValueError, match="strategy"):
        os_.SignalRecord(
            decision_ts_ms=1,
            strategy="AP-A3",
            config_id="x",
            symbol="AAA/USD",
            action="NO_ACTION",
            target_notional=0.0,
            reason_code="NO_ENTRY_SIGNAL",
            evidence_hash="x",
        )


def test_evidence_hash_is_deterministic_and_1_ulp_sensitive():
    h1 = os_.evidence_hash({"d": 0.005, "r": 0.01})
    h2 = os_.evidence_hash({"d": 0.005, "r": 0.01})
    h3 = os_.evidence_hash({"d": 0.005 + 1e-17, "r": 0.01})
    assert h1 == h2
    assert h1 != h3


def test_canonical_sort_orders_by_decision_ts_strategy_config_id_symbol():
    records = [
        _record(decision_ts_ms=2, symbol="ZZZ/USD"),
        _record(decision_ts_ms=1, symbol="BBB/USD"),
        _record(decision_ts_ms=1, symbol="AAA/USD"),
        _record(decision_ts_ms=1, config_id="AP-A1-01", symbol="AAA/USD"),
    ]
    ordered = os_.canonical_sort(records)
    keys = [(r.decision_ts_ms, r.strategy, r.config_id, r.symbol) for r in ordered]
    assert keys == sorted(keys)


def test_canonical_sort_is_invariant_to_input_container_permutation():
    base = [_record(symbol=s) for s in ("CCC/USD", "AAA/USD", "BBB/USD")]
    shuffled = list(base)
    random.Random(42).shuffle(shuffled)
    assert os_.canonical_sort(base) == os_.canonical_sort(shuffled)
