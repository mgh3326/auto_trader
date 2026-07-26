"""ROB-1062 H4 (AC25, AC28) — the PnL-blind counts: entries, turnover,
holding-period distribution, and the rejection-reason histogram. These are
ALWAYS exposed, mask or no mask (never wrapped by ``oos_mask.Masked`` — see
that module's own docstring and ``tests/test_oos_mask.py``'s "opposite of
masked" documentation test) — this is H5's dry-count gate's entire input
surface.

AC28: an all-zero-trades fold is a normal, legitimate outcome and must never
be hidden — but a rejection histogram with ZERO entries when records exist
is ``incomplete`` (the ROB-1025 lesson: an empty reason histogram alongside
real decisions means the histogram-plumbing itself is broken, not that the
strategy chose not to trade). ``BlindCounts.is_incomplete`` makes this
distinction explicit and queryable rather than left for a caller to notice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import reason_codes as rc
from output_schema import SignalRecord
from trade_ledger import FillAttempt, Trade

__all__ = [
    "BlindCounts",
    "annualized_stress_cost_pct",
    "compute_blind_counts",
]


@dataclass(frozen=True)
class BlindCounts:
    total_decision_records: int
    modeled_entries_count: int
    closed_trades_count: int
    open_positions_count: int
    entry_unfilled_count: int
    exit_unfilled_count: int
    fill_window_incomplete_count: int
    holding_days: tuple[int, ...]
    reason_code_histogram: dict[str, int]

    @property
    def is_incomplete(self) -> bool:
        """ROB-1025 lesson: real decision records with an EMPTY reason
        histogram means the histogram plumbing broke, not that nothing
        happened — never silently collapse this into a clean zero-trade
        result."""
        return (
            self.total_decision_records > 0
            and sum(self.reason_code_histogram.values()) == 0
        )


def compute_blind_counts(
    records: Sequence[SignalRecord],
    *,
    closed_trades: Sequence[Trade] = (),
    open_positions_count: int = 0,
    fill_attempts: Sequence[FillAttempt] = (),
) -> BlindCounts:
    """``records``/``closed_trades``/``fill_attempts`` must already be
    scoped to the phase (TRAIN or OOS) the caller wants counted — this
    function does no phase attribution itself (that is the walk-forward
    runner's job, by ``entry_decision_ts_ms``/``decision_ts_ms`` membership;
    see ``runner.py``'s module docstring for why a SINGLE canonical ledger
    run over the full continuous stream, filtered afterward by timestamp,
    is the only safe way to do this — re-running the ledger on a
    phase-sliced record subsequence can orphan a EXIT whose paired ENTER
    fell in the other phase)."""
    closed_trades_count = len(closed_trades)
    modeled_entries_count = closed_trades_count + open_positions_count
    holding_days = tuple(t.holding_days for t in closed_trades)
    histogram = rc.reconcile_histogram([r.reason_code for r in records])
    entry_unfilled = sum(
        1
        for a in fill_attempts
        if a.leg == "ENTRY" and a.outcome.reason == "ENTRY_UNFILLED"
    )
    exit_unfilled = sum(
        1
        for a in fill_attempts
        if a.leg == "EXIT" and a.outcome.reason == "EXIT_UNFILLED"
    )
    incomplete = sum(
        1 for a in fill_attempts if a.outcome.reason == "FILL_WINDOW_INCOMPLETE"
    )
    return BlindCounts(
        total_decision_records=len(records),
        modeled_entries_count=modeled_entries_count,
        closed_trades_count=closed_trades_count,
        open_positions_count=open_positions_count,
        entry_unfilled_count=entry_unfilled,
        exit_unfilled_count=exit_unfilled,
        fill_window_incomplete_count=incomplete,
        holding_days=holding_days,
        reason_code_histogram=histogram,
    )


def annualized_stress_cost_pct(
    *, modeled_entries_count: int, window_days: int, cost_bp: float
) -> float:
    """PnL-blind annualized turnover-cost drag, as a percentage of NAV
    (Run A SS5/SS11.6/SS12.6's "stress 드래그 연 X%"): entries-per-year times
    the (stress) cost scenario's bp, expressed as a percent. Uses ONLY
    entry counts and the sealed cost scenario bp — never a PnL figure —
    which is exactly what lets AC9's cost cap reject a config "PnL-blind,
    before any gross edge is even looked at"."""
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    entries_per_year = modeled_entries_count / window_days * 365.0
    return entries_per_year * (cost_bp / 10_000.0) * 100.0
