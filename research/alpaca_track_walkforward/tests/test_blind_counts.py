"""ROB-1062 H4 (AC25, AC28) — PnL-blind counts."""

from __future__ import annotations

import blind_counts as bc
import fill_model as fm
import pytest
import trade_ledger as tl
from output_schema import SignalRecord, evidence_hash


def _rec(ts, action, reason):
    return SignalRecord(
        decision_ts_ms=ts,
        strategy="AP-A1",
        config_id="AP-A1-00",
        symbol="BTC/USD",
        action=action,
        target_notional=0.0,
        reason_code=reason,
        evidence_hash=evidence_hash({}),
    )


def test_annualized_stress_cost_pct_matches_frozen_notional_formula():
    result = bc.annualized_stress_cost_pct(
        entry_filled_notionals=(62.5,) * 10,
        window_days=365,
        nav_usd=2000.0,
        cost_bp=120.0,
    )
    assert result == pytest.approx(10 * (62.5 / 2000.0) * 0.012 * 100.0)


def test_annualized_stress_cost_pct_rejects_non_positive_window():
    with pytest.raises(ValueError, match="window_days"):
        bc.annualized_stress_cost_pct(
            entry_filled_notionals=(62.5,),
            window_days=0,
            nav_usd=2000.0,
            cost_bp=120.0,
        )


def _fractional_entry_notionals(
    entry_count: float, full_size_notional: float
) -> tuple[float, ...]:
    whole = int(entry_count)
    fraction = entry_count - whole
    notionals = [full_size_notional] * whole
    if fraction:
        notionals.append(full_size_notional * fraction)
    return tuple(notionals)


@pytest.mark.parametrize(
    ("entry_count", "full_size_notional", "expected_pct"),
    [
        (365 * 24 * 0.008, 2000.0 / 32, 2.628),
        (365 * 24 * 0.015, 2000.0 / 32, 4.9275),
        (52 * 5 * 0.21, 2000.0 / 5, 13.104),
        (52 * 5 * 0.29, 2000.0 / 5, 18.096),
    ],
)
def test_frozen_formula_reproduces_preregistered_drag_ranges(
    entry_count, full_size_notional, expected_pct
):
    result = bc.annualized_stress_cost_pct(
        entry_filled_notionals=_fractional_entry_notionals(
            entry_count, full_size_notional
        ),
        window_days=365,
        nav_usd=2000.0,
        cost_bp=120.0,
    )
    assert result == pytest.approx(expected_pct)


@pytest.mark.parametrize(
    ("full_size_notional", "expected_pct"),
    [
        (2000.0 / 32, 1.125),
        (2000.0 / 5, 7.2),
        (2000.0 / 6, 6.0),
    ],
)
def test_thirty_full_size_entries_are_compatible_with_frozen_caps(
    full_size_notional, expected_pct
):
    result = bc.annualized_stress_cost_pct(
        entry_filled_notionals=(full_size_notional,) * 30,
        window_days=365,
        nav_usd=2000.0,
        cost_bp=120.0,
    )
    assert result == pytest.approx(expected_pct)


def test_full_nav_per_entry_misread_is_rejected_not_used_by_the_api():
    full_nav_drag = bc.annualized_stress_cost_pct(
        entry_filled_notionals=(2000.0,) * 30,
        window_days=365,
        nav_usd=2000.0,
        cost_bp=120.0,
    )
    assert full_nav_drag == pytest.approx(36.0)
    with pytest.raises(TypeError):
        bc.annualized_stress_cost_pct(
            modeled_entries_count=30,  # type: ignore[call-arg]
            window_days=365,
            cost_bp=120.0,
        )


def _fill_attempt(ts, *, symbol="BTC/USD", leg, reason):
    outcome = fm.FillOutcome(
        filled=False, fill_price=None, fill_bar_offset=None, reason=reason
    )
    return tl.FillAttempt(decision_ts_ms=ts, symbol=symbol, leg=leg, outcome=outcome)


def test_compute_blind_counts_aggregates_across_fill_attempts():
    records = [
        _rec(1, "NO_ACTION", "NO_ENTRY_SIGNAL"),
        _rec(2, "NO_ACTION", "UNIVERSE_INELIGIBLE"),
    ]
    attempts = [_fill_attempt(1, leg="ENTRY", reason="ENTRY_UNFILLED")]
    counts = bc.compute_blind_counts(records, fill_attempts=attempts)
    assert counts.total_decision_records == 2
    assert counts.entry_unfilled_count == 1
    assert counts.reason_code_histogram == {
        "NO_ENTRY_SIGNAL": 1,
        "UNIVERSE_INELIGIBLE": 1,
    }
    assert counts.is_incomplete is False


def test_zero_trades_with_real_records_and_a_populated_histogram_is_not_incomplete():
    records = [_rec(1, "NO_ACTION", "NO_ENTRY_SIGNAL")]
    counts = bc.compute_blind_counts(records)
    assert counts.modeled_entries_count == 0
    assert counts.is_incomplete is False  # legitimate all-NO_ACTION fold


def test_empty_histogram_alongside_real_records_is_incomplete_rob_1025_lesson():
    """The exact ROB-1025 failure mode: real decision records exist, but the
    histogram plumbing produced nothing — must be flagged, never mistaken
    for a clean zero-trade result."""
    records = [_rec(1, "NO_ACTION", "NO_ENTRY_SIGNAL")]
    counts = bc.BlindCounts(
        total_decision_records=len(records),
        modeled_entries_count=0,
        closed_trades_count=0,
        open_positions_count=0,
        entry_unfilled_count=0,
        exit_unfilled_count=0,
        fill_window_incomplete_count=0,
        holding_days=(),
        reason_code_histogram={},  # broken plumbing -- empty despite real records
    )
    assert counts.is_incomplete is True


def test_no_records_at_all_is_not_incomplete_there_is_nothing_to_summarize():
    counts = bc.BlindCounts(
        total_decision_records=0,
        modeled_entries_count=0,
        closed_trades_count=0,
        open_positions_count=0,
        entry_unfilled_count=0,
        exit_unfilled_count=0,
        fill_window_incomplete_count=0,
        holding_days=(),
        reason_code_histogram={},
    )
    assert counts.is_incomplete is False
