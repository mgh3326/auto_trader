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


def test_evidence_hash_pins_every_field_not_only_d():
    # A8/A8b (ROB-1061 adversarial-verification): the test above only ever
    # perturbs `d` -- a mutant that narrowed `evidence_hash` to hash ONLY the
    # `d` key (silently dropping `symbol`/`r`/`threshold`/anything else the
    # caller supplied) would still pass it. Perturb every OTHER field
    # independently, holding `d` fixed, and prove each one alone moves the
    # hash -- this is the actual "full evidence field set is pinned"
    # property, not just "d is pinned".
    base = {"symbol": "AAA/USD", "d": 0.005, "r": 0.01, "threshold": 0.005}
    baseline = os_.evidence_hash(base)
    for key, replacement in (
        ("symbol", "ZZZ/USD"),
        ("r", 0.02),
        ("threshold", 0.0001),
    ):
        mutated = dict(base)
        mutated[key] = replacement
        assert os_.evidence_hash(mutated) != baseline, (
            f"changing {key!r} alone (with every other field held fixed) "
            "must move the evidence_hash"
        )


def test_evidence_hash_is_sensitive_to_a_dropped_field_not_only_a_changed_one():
    # A dropped key (evidence built by a caller that forgot a field) must
    # ALSO move the hash -- distinct from "a changed value moves it".
    with_threshold = {"symbol": "AAA/USD", "d": 0.005, "threshold": 0.005}
    without_threshold = {"symbol": "AAA/USD", "d": 0.005}
    assert os_.evidence_hash(with_threshold) != os_.evidence_hash(without_threshold)


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
