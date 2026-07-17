"""ROB-944 (H4, ROB-940) — data-gap-in-position rejection + funding PIT entry
gate tests.

Q2/Q3 (orch-fable-answer-rob944-20260717.md, final): H1's half-open
``[entry, exit)`` position window is authoritative; the entry funding gate
uses ONLY the last-known rate/interval before entry (no lookahead), rejects
only when the SIGNED expected cost is strictly greater than 3.0bp (exactly
3.0bp remains eligible), and reports ``funding_evidence_unavailable`` /
``expected_funding_cost_above_3bps`` as separate, stable reason codes.
"""

from __future__ import annotations

from funding_oi_archive import FundingRow
from rob940_engine import TradeRecord
from rob941_funding_sidecar import FundingSidecar
from rob944_gap_funding import (
    FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS,
    REASON_DATA_GAP_IN_POSITION,
    REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
    REASON_FUNDING_EVIDENCE_UNAVAILABLE,
    build_funding_lookup,
    evaluate_funding_entry_gate,
    is_trade_gap_in_position,
)

_HOUR_MS = 3_600_000


def _trade(entry_ts=10_000, exit_ts=20_000):
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
        net_bps=90.0,
        fold_id="fold-00",
    )


# ---------------------------------------------------------------------------
# data-gap-in-position
# ---------------------------------------------------------------------------


def test_trade_with_no_gap_overlap_is_not_flagged():
    trade = _trade(entry_ts=10_000, exit_ts=20_000)
    assert is_trade_gap_in_position(trade, [(30_000, 40_000)]) is False


def test_trade_whose_window_overlaps_a_gap_is_flagged():
    trade = _trade(entry_ts=10_000, exit_ts=20_000)
    assert is_trade_gap_in_position(trade, [(15_000, 16_000)]) is True


def test_trade_gap_reason_constant_is_stable():
    assert REASON_DATA_GAP_IN_POSITION == "rejected:data_gap_in_position"


# ---------------------------------------------------------------------------
# funding PIT entry gate
# ---------------------------------------------------------------------------


def test_funding_gate_constant_is_frozen_at_3_bps():
    assert FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS == 3.0


def test_no_known_rate_before_entry_fails_closed_as_evidence_unavailable():
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=50_000, funding_interval_hours=8, last_funding_rate=0.0001
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=10_000, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is False
    assert result.rejection_reason == REASON_FUNDING_EVIDENCE_UNAVAILABLE
    assert result.expected_cost_bps is None


def test_nan_last_funding_rate_fails_closed_not_fail_open():
    """Captain correction: round(nan, 8) > 3.0 is False in Python, so a bare
    arithmetic check would silently PASS a NaN rate (fail-open). The gate
    must explicitly reject non-finite rates as funding_evidence_unavailable."""
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts,
                funding_interval_hours=8,
                last_funding_rate=float("nan"),
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is False
    assert result.rejection_reason == REASON_FUNDING_EVIDENCE_UNAVAILABLE
    assert result.expected_cost_bps is None


def test_positive_infinity_last_funding_rate_fails_closed():
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts,
                funding_interval_hours=8,
                last_funding_rate=float("inf"),
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is False
    assert result.rejection_reason == REASON_FUNDING_EVIDENCE_UNAVAILABLE


def test_negative_infinity_last_funding_rate_fails_closed():
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts,
                funding_interval_hours=8,
                last_funding_rate=float("-inf"),
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is False
    assert result.rejection_reason == REASON_FUNDING_EVIDENCE_UNAVAILABLE


def test_no_relevant_crossing_before_deadline_passes_with_zero_expected_cost():
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts - 1000,
                funding_interval_hours=8,
                last_funding_rate=0.01,
            )
        ],
    )
    # max_hold shorter than time to next crossing (8h away) -> not relevant.
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=1 * _HOUR_MS
    )
    assert result.passed is True
    assert result.rejection_reason is None
    assert result.expected_cost_bps == 0.0


def test_expected_cost_exactly_3bps_remains_eligible_inclusive_boundary():
    entry_ts = 0
    # rate 0.0003 -> 0.0003*1e4 = 3.0bp signed cost for longs (positive rate).
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts, funding_interval_hours=8, last_funding_rate=0.0003
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is True
    assert result.rejection_reason is None
    assert result.expected_cost_bps == 3.0


def test_expected_cost_above_3bps_for_long_is_rejected():
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts, funding_interval_hours=8, last_funding_rate=0.0004
            )
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is False
    assert result.rejection_reason == REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS
    assert result.expected_cost_bps == 4.0


def test_negative_signed_cost_ie_a_credit_never_rejects_regardless_of_magnitude():
    """The gate compares the SIGNED expected cost, not its magnitude -- a
    position expected to RECEIVE funding (a credit) must never be rejected,
    however large the credit is."""
    entry_ts = 0
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=entry_ts, funding_interval_hours=8, last_funding_rate=0.01
            )
        ],
    )
    # short side flips sign: positive rate -> shorts RECEIVE -> negative signed cost.
    result = evaluate_funding_entry_gate(
        sidecar, side="short", entry_ts_ms=entry_ts, max_hold_ms=8 * _HOUR_MS
    )
    assert result.passed is True
    assert result.expected_cost_bps == -100.0


def test_gate_uses_only_last_known_rate_no_lookahead():
    """A LATER rate change already present in the sidecar (after entry_ts)
    must NOT influence the entry gate's decision -- only the rate known
    at/before entry_ts is consulted."""
    entry_ts = 100_000
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=90_000, funding_interval_hours=8, last_funding_rate=0.0001
            ),
            # A future, much larger rate becomes known AFTER entry_ts.
            FundingRow(
                calc_time=200_000, funding_interval_hours=8, last_funding_rate=0.05
            ),
        ],
    )
    result = evaluate_funding_entry_gate(
        sidecar, side="long", entry_ts_ms=entry_ts, max_hold_ms=1 * _HOUR_MS
    )
    # Only the 0.0001 row (known at/before entry_ts) may be consulted; the
    # 0.05 future row must never leak into this decision.
    assert result.expected_cost_bps != 0.05 * 1e4
    assert result.passed is True  # next crossing (8h away) is beyond the 1h max hold


def test_build_funding_lookup_delegates_to_h1_half_open_window_without_reinterpretation():
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=10_000, funding_interval_hours=8, last_funding_rate=0.0001
            ),  # at entry: included
            FundingRow(
                calc_time=20_000, funding_interval_hours=8, last_funding_rate=0.0002
            ),  # at exit: excluded
            FundingRow(
                calc_time=15_000, funding_interval_hours=8, last_funding_rate=0.0003
            ),  # strictly inside: included
        ],
    )
    lookup = build_funding_lookup({"BTCUSDT": sidecar})
    crossings = lookup("BTCUSDT", "long", 10_000, 20_000)
    assert {c.ts for c in crossings} == {10_000, 15_000}
    assert 20_000 not in {c.ts for c in crossings}


def test_build_funding_lookup_returns_empty_for_unknown_symbol():
    lookup = build_funding_lookup({})
    assert lookup("BTCUSDT", "long", 10_000, 20_000) == ()
