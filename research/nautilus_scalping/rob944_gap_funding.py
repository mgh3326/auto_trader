"""ROB-944 (H4, ROB-940) — data-gap-in-position rejection + funding PIT entry
gate (pure, stdlib).

Frozen by ``orch-fable-answer-rob944-20260717.md`` (Q2/Q3, final):

* Q2 -- H1's ``rob941_funding_sidecar.FundingSidecar.realized_crossings``
  half-open ``[entry_ts, exit_ts)`` position window is authoritative.
  ``build_funding_lookup`` delegates to it directly and never reinterprets
  the endpoint convention (the stale ``rob940_cost_model.FundingCrossing``
  field comment was corrected in this PR to match).
* Q3 -- the entry funding gate, evaluated once at the resolved entry time
  ``E`` (never re-evaluated with a later-known rate):
  1. fetch ``last_known_rate(E)``; missing (or an invalid non-positive
     interval) fails closed with ``funding_evidence_unavailable``;
  2. compute the first expected crossing strictly after ``E`` by repeatedly
     adding that row's own ``funding_interval_hours`` (in ms) to its
     ``calc_time``;
  3. a crossing is relevant only if it falls in ``(E, E + max_hold_ms]``
     (the position's maximum holding deadline for THIS signal's
     ``timeout_bars``); otherwise the expected cost is exactly ``0.0``;
  4. the SIGNED expected cost is ``last_known_rate * 1e4`` for longs, negated
     for shorts (positive => longs pay shorts, matching
     ``rob940_cost_model.realized_funding_bps``'s sign convention);
  5. reject ONLY when that signed value is strictly greater than
     ``FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS`` (3.0bp) -- exactly 3.0bp
     remains eligible, and a negative signed value (an expected CREDIT, e.g.
     a short receiving positive-rate funding) never rejects regardless of its
     magnitude, since the gate exists to block expected PAYMENTS, not credits;
  6. the two rejection reasons are stable and reported/aggregated separately.

``is_trade_gap_in_position`` reuses H1's own ``rob941_gaps.position_touches_gap``
predicate verbatim (no reimplementation) to flag ``rejected:data_gap_in_position``.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
sibling rob941_* modules, deterministic given its input.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

import rob941_gaps as gaps
from rob940_cost_model import FundingCrossing, Side
from rob940_engine import TradeRecord
from rob941_funding_sidecar import FundingSidecar

_MS_PER_HOUR = 3_600_000

FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS = 3.0
REASON_FUNDING_EVIDENCE_UNAVAILABLE = "funding_evidence_unavailable"
REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS = "expected_funding_cost_above_3bps"
REASON_DATA_GAP_IN_POSITION = "rejected:data_gap_in_position"


def is_trade_gap_in_position(
    trade: TradeRecord, gap_ranges: Sequence[tuple[int, int]]
) -> bool:
    """True iff ``trade``'s ``[entry_ts, exit_ts)`` window overlaps ANY gap
    range -- delegates verbatim to H1's ``position_touches_gap``."""
    return gaps.position_touches_gap(trade.entry_ts, trade.exit_ts, list(gap_ranges))


@dataclass(frozen=True)
class FundingEntryGateResult:
    passed: bool
    rejection_reason: str | None
    expected_cost_bps: float | None  # None iff funding_evidence_unavailable


def evaluate_funding_entry_gate(
    sidecar: FundingSidecar,
    *,
    side: Side,
    entry_ts_ms: int,
    max_hold_ms: int,
) -> FundingEntryGateResult:
    """PIT-safe entry funding gate (Q3, final). Uses ONLY the rate known
    at/before ``entry_ts_ms`` -- a later-known rate change can never affect
    this decision, by construction of ``FundingSidecar.last_known_rate``.
    """
    last_row = sidecar.last_known_rate(entry_ts_ms)
    if (
        last_row is None
        or last_row.funding_interval_hours <= 0
        or not math.isfinite(last_row.last_funding_rate)
    ):
        # Captain correction (2026-07-17): a NaN last_funding_rate previously
        # fell through to `round(nan, 8) > 3.0` -- always False in Python,
        # which silently PASSED the gate (fail-OPEN) instead of treating a
        # malformed/non-finite known rate as unusable evidence.
        return FundingEntryGateResult(
            passed=False,
            rejection_reason=REASON_FUNDING_EVIDENCE_UNAVAILABLE,
            expected_cost_bps=None,
        )

    interval_ms = last_row.funding_interval_hours * _MS_PER_HOUR
    deadline_ms = entry_ts_ms + max_hold_ms
    next_crossing_ms = last_row.calc_time
    while next_crossing_ms <= entry_ts_ms:
        next_crossing_ms += interval_ms

    if not (entry_ts_ms < next_crossing_ms <= deadline_ms):
        return FundingEntryGateResult(
            passed=True, rejection_reason=None, expected_cost_bps=0.0
        )

    signed_rate_bps = last_row.last_funding_rate * 1e4
    expected_cost_bps = signed_rate_bps if side == "long" else -signed_rate_bps
    # Rounded to 1e-8 bp to strip binary-float representation noise (e.g.
    # 0.0003*1e4 lands a few ULPs below 3.0) without masking any real
    # boundary distinction -- same treatment as
    # rob940_signal_s2._evaluate_target_gates's d_tp_bps/r_min_sl_bps.
    expected_cost_bps = round(expected_cost_bps, 8)

    if expected_cost_bps > FUNDING_ENTRY_GATE_MAX_EXPECTED_COST_BPS:
        return FundingEntryGateResult(
            passed=False,
            rejection_reason=REASON_EXPECTED_FUNDING_COST_ABOVE_3BPS,
            expected_cost_bps=expected_cost_bps,
        )
    return FundingEntryGateResult(
        passed=True, rejection_reason=None, expected_cost_bps=expected_cost_bps
    )


def build_funding_lookup(sidecars: dict[str, FundingSidecar]):
    """Build the ``funding_lookup`` callable ``rob940_engine.run_symbol_stream``
    expects: ``(symbol, side, entry_ts, exit_ts) -> Sequence[FundingCrossing]``.

    Delegates straight to H1's ``FundingSidecar.realized_crossings`` (Q2's
    frozen ``[entry, exit)`` window) -- no second crossing implementation.
    """

    def _lookup(
        symbol: str, side: Side, entry_ts: int, exit_ts: int
    ) -> tuple[FundingCrossing, ...]:
        sidecar = sidecars.get(symbol)
        if sidecar is None:
            return ()
        rows = sidecar.realized_crossings(entry_ts, exit_ts)
        return tuple(
            FundingCrossing(ts=r.calc_time, rate_bps=r.last_funding_rate * 1e4)
            for r in rows
        )

    return _lookup
