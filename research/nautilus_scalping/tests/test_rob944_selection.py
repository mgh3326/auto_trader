"""ROB-944 (H4, ROB-940) — fold train-selection authority tests.

Selection authority (orch-fable-answer-strategy-20260717.md, final): eligible-
symbol EQUAL-WEIGHT MEAN of per-symbol TRAIN net expectancy (bps/trade) at
the independent 17bp primary-stress run is the ONLY selection/pass authority.
Trade-level POOLED expectancy is report-only and must never rank candidates.
Symbols with <5 completed train trades are excluded
(``insufficient_symbol_evidence``); a config with <2 eligible symbols is
``rejected:insufficient_train_evidence``. Tie-break: profit factor, then
canonical config_id ascending.
"""

from __future__ import annotations

import math

import pytest
from rob944_selection import (
    INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON,
    INSUFFICIENT_SYMBOL_EVIDENCE_REASON,
    MIN_ELIGIBLE_SYMBOLS,
    MIN_SYMBOL_TRAIN_TRADES,
    ConfigTrainCandidate,
    SymbolTrainEvidence,
    evaluate_config_candidate,
    select_fold_config,
)


def _ev(symbol, trades, expectancy, profit=None, loss=None, train_artifact_hash=None):
    if profit is None and loss is None:
        profit = max(expectancy, 0.0) * trades
        loss = max(-expectancy, 0.0) * trades
    if train_artifact_hash is None:
        train_artifact_hash = f"artifact-{symbol}-{trades}-{expectancy}"
    return SymbolTrainEvidence(
        symbol=symbol,
        completed_trades=trades,
        net_expectancy_bps=expectancy,
        gross_profit_bps=profit,
        gross_loss_bps=loss,
        train_artifact_hash=train_artifact_hash,
    )


def _twelve_candidates(winner_id="S1-00", **overrides):
    """12 uniquely-ided, trivially-eligible candidates; ``overrides`` maps
    config_id -> tuple[SymbolTrainEvidence, ...] to replace the default."""
    candidates = []
    for i in range(12):
        config_id = f"S1-{i:02d}"
        if config_id in overrides:
            evidence = overrides[config_id]
        else:
            evidence = (_ev("BTCUSDT", 10, 1.0), _ev("XRPUSDT", 10, 1.0))
        candidates.append(
            ConfigTrainCandidate(config_id=config_id, symbol_evidence=evidence)
        )
    return candidates


def test_min_symbol_train_trades_and_min_eligible_symbols_constants():
    assert MIN_SYMBOL_TRAIN_TRADES == 5
    assert MIN_ELIGIBLE_SYMBOLS == 2


def test_symbol_below_five_trades_is_excluded_as_insufficient_evidence():
    candidate = ConfigTrainCandidate(
        config_id="S1-00",
        symbol_evidence=(
            _ev("BTCUSDT", 4, 10.0),
            _ev("XRPUSDT", 10, 5.0),
            _ev("DOGEUSDT", 10, 5.0),
        ),
    )
    outcome = evaluate_config_candidate(candidate)
    assert outcome.rejected is False
    assert "BTCUSDT" not in outcome.eligible_symbols
    assert ("BTCUSDT", INSUFFICIENT_SYMBOL_EVIDENCE_REASON) in outcome.excluded_symbols
    assert outcome.eligible_symbols == ("XRPUSDT", "DOGEUSDT")


def test_exactly_five_trades_is_eligible_not_excluded():
    candidate = ConfigTrainCandidate(
        config_id="S1-00",
        symbol_evidence=(_ev("BTCUSDT", 5, 10.0), _ev("XRPUSDT", 10, 5.0)),
    )
    outcome = evaluate_config_candidate(candidate)
    assert outcome.excluded_symbols == ()
    assert outcome.eligible_symbols == ("BTCUSDT", "XRPUSDT")


def test_fewer_than_two_eligible_symbols_rejects_the_config():
    candidate = ConfigTrainCandidate(
        config_id="S1-00",
        symbol_evidence=(
            _ev("BTCUSDT", 4, 10.0),  # excluded
            _ev("XRPUSDT", 10, 5.0),  # only 1 eligible symbol remains
        ),
    )
    outcome = evaluate_config_candidate(candidate)
    assert outcome.rejected is True
    assert outcome.rejection_reason == INSUFFICIENT_ELIGIBLE_SYMBOLS_REASON
    assert outcome.equal_weight_expectancy_bps is None


def test_equal_weight_mean_is_unweighted_average_across_eligible_symbols():
    candidate = ConfigTrainCandidate(
        config_id="S1-00",
        symbol_evidence=(
            _ev("BTCUSDT", 1000, 10.0),
            _ev("XRPUSDT", 5, -6.0),
        ),
    )
    outcome = evaluate_config_candidate(candidate)
    assert outcome.equal_weight_expectancy_bps == pytest.approx(2.0)  # (10 + -6) / 2


def test_pooled_expectancy_is_report_only_never_the_selection_authority_RED_fixture():
    """The decisive RED fixture (H4 prompt item 6): pooled and equal-weight
    expectancy must rank two configs OPPOSITELY, and the equal-weight winner
    must be the one selected -- pooled expectancy must never be consulted by
    ``select_fold_config``.
    """
    # Config A: a high-volume symbol dominates pooled expectancy upward.
    config_a = ConfigTrainCandidate(
        config_id="S1-00",
        symbol_evidence=(_ev("BTCUSDT", 100, 10.0), _ev("XRPUSDT", 5, -5.0)),
    )
    # Config B: both symbols equal, lower pooled number but higher equal-weight.
    config_b = ConfigTrainCandidate(
        config_id="S1-01",
        symbol_evidence=(_ev("BTCUSDT", 100, 5.0), _ev("XRPUSDT", 5, 5.0)),
    )
    outcome_a = evaluate_config_candidate(config_a)
    outcome_b = evaluate_config_candidate(config_b)

    # Equal-weight: A = (10-5)/2 = 2.5 ; B = (5+5)/2 = 5.0 -> B wins.
    assert outcome_a.equal_weight_expectancy_bps == pytest.approx(2.5)
    assert outcome_b.equal_weight_expectancy_bps == pytest.approx(5.0)
    assert outcome_b.equal_weight_expectancy_bps > outcome_a.equal_weight_expectancy_bps

    # Pooled (trade-count-weighted, report-only): A = (100*10 + 5*-5)/105 ~= 9.29
    # ; B = (100*5 + 5*5)/105 = 5.0 -> pooled would rank A ABOVE B (opposite order).
    assert outcome_a.pooled_expectancy_bps == pytest.approx((1000 - 25) / 105)
    assert outcome_b.pooled_expectancy_bps == pytest.approx(5.0)
    assert outcome_a.pooled_expectancy_bps > outcome_b.pooled_expectancy_bps

    candidates = _twelve_candidates(
        **{"S1-00": config_a.symbol_evidence, "S1-01": config_b.symbol_evidence}
    )
    trace = select_fold_config("S1", candidates)
    assert trace.selected_config_id == "S1-01"  # the EQUAL-WEIGHT winner, not pooled


def test_tie_break_by_profit_factor_when_equal_weight_expectancy_ties():
    evidence_high_pf = (
        _ev("BTCUSDT", 10, 5.0, profit=100.0, loss=10.0),
        _ev("XRPUSDT", 10, 5.0, profit=100.0, loss=10.0),
    )
    evidence_low_pf = (
        _ev("BTCUSDT", 10, 5.0, profit=60.0, loss=50.0),
        _ev("XRPUSDT", 10, 5.0, profit=60.0, loss=50.0),
    )
    candidates = _twelve_candidates(
        **{"S1-00": evidence_low_pf, "S1-01": evidence_high_pf}
    )
    trace = select_fold_config("S1", candidates)
    outcome_00 = next(o for o in trace.candidates if o.config_id == "S1-00")
    outcome_01 = next(o for o in trace.candidates if o.config_id == "S1-01")
    assert (
        outcome_00.equal_weight_expectancy_bps == outcome_01.equal_weight_expectancy_bps
    )
    assert outcome_01.profit_factor > outcome_00.profit_factor
    assert trace.selected_config_id == "S1-01"


def test_tie_break_by_canonical_config_id_ascending_when_expectancy_and_pf_tie():
    evidence = (_ev("BTCUSDT", 10, 5.0), _ev("XRPUSDT", 10, 5.0))
    candidates = _twelve_candidates(**{"S1-07": evidence, "S1-03": evidence})
    # Force every OTHER candidate to be worse so 03/07 are the true top-2 tie.
    trace = select_fold_config("S1", candidates)
    assert trace.selected_config_id == "S1-03"  # ascending config_id wins the tie


def test_select_fold_config_rejects_wrong_candidate_count():
    candidates = _twelve_candidates()[:11]
    with pytest.raises(ValueError):
        select_fold_config("S1", candidates)


def test_select_fold_config_rejects_duplicate_config_id():
    candidates = _twelve_candidates()
    mutated = list(candidates[:-1]) + [
        ConfigTrainCandidate(
            config_id=candidates[0].config_id,  # duplicate of candidates[0]
            symbol_evidence=candidates[0].symbol_evidence,
        )
    ]
    with pytest.raises(ValueError):
        select_fold_config("S1", mutated)


def test_select_fold_config_rejects_a_13th_config():
    candidates = _twelve_candidates() + [
        ConfigTrainCandidate(
            config_id="S1-12",
            symbol_evidence=(_ev("BTCUSDT", 10, 1.0), _ev("XRPUSDT", 10, 1.0)),
        )
    ]
    with pytest.raises(ValueError):
        select_fold_config("S1", candidates)


def test_symbol_evidence_rejects_non_finite_expectancy():
    with pytest.raises(ValueError):
        SymbolTrainEvidence(
            symbol="BTCUSDT",
            completed_trades=10,
            net_expectancy_bps=math.nan,
            gross_profit_bps=0.0,
            gross_loss_bps=0.0,
            train_artifact_hash="a" * 64,
        )
    with pytest.raises(ValueError):
        SymbolTrainEvidence(
            symbol="BTCUSDT",
            completed_trades=10,
            net_expectancy_bps=math.inf,
            gross_profit_bps=0.0,
            gross_loss_bps=0.0,
            train_artifact_hash="a" * 64,
        )


def test_symbol_evidence_rejects_empty_train_artifact_hash():
    with pytest.raises(ValueError):
        SymbolTrainEvidence(
            symbol="BTCUSDT",
            completed_trades=10,
            net_expectancy_bps=1.0,
            gross_profit_bps=1.0,
            gross_loss_bps=0.0,
            train_artifact_hash="",
        )


def test_config_train_candidate_rejects_duplicate_symbol_evidence():
    with pytest.raises(ValueError):
        ConfigTrainCandidate(
            config_id="S1-00",
            symbol_evidence=(_ev("BTCUSDT", 10, 1.0), _ev("BTCUSDT", 10, 1.0)),
        )


def test_all_configs_rejected_yields_no_selection():
    candidates = _twelve_candidates(
        **{
            f"S1-{i:02d}": (_ev("BTCUSDT", 1, 1.0),)  # only 1 eligible symbol
            for i in range(12)
        }
    )
    trace = select_fold_config("S1", candidates)
    assert trace.selected_config_id is None
    assert all(o.rejected for o in trace.candidates)


def test_selection_trace_preserves_per_config_train_input_hash():
    candidates = _twelve_candidates()
    trace = select_fold_config("S1", candidates)
    hashes = [o.train_input_hash for o in trace.candidates]
    assert len(hashes) == 12
    assert all(isinstance(h, str) and len(h) == 64 for h in hashes)
    # Distinguishable inputs must NOT collide.
    distinct_candidates = _twelve_candidates(
        **{"S1-00": (_ev("BTCUSDT", 999, 42.0), _ev("XRPUSDT", 999, 42.0))}
    )
    distinct_trace = select_fold_config("S1", distinct_candidates)
    assert (
        distinct_trace.candidates[0].train_input_hash
        != trace.candidates[0].train_input_hash
    )


def test_selection_trace_orders_candidates_by_input_order_not_by_score():
    candidates = _twelve_candidates()
    trace = select_fold_config("S1", candidates)
    assert [o.config_id for o in trace.candidates] == [c.config_id for c in candidates]
