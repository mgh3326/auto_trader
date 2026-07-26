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
    modeled_entries_count: int | None = None,
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
    if modeled_entries_count is None:
        modeled_entries_count = closed_trades_count + open_positions_count
    if type(modeled_entries_count) is not int or modeled_entries_count < 0:
        raise ValueError("modeled_entries_count must be a non-negative built-in int")
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
    *,
    entry_filled_notionals: Sequence[float],
    window_days: int,
    nav_usd: float,
    cost_bp: float,
) -> float:
    """Annualized notional-weighted drag as percentage of fixed NAV.

    Audit freeze 2026-07-26:
      100 * (365 / D_W) * sum((entry_filled_notional / NAV0) * cost_rate)
    """
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if type(nav_usd) is not float or nav_usd <= 0.0:
        raise ValueError("nav_usd must be a positive built-in float")
    if type(cost_bp) is not float or cost_bp <= 0.0:
        raise ValueError("cost_bp must be a positive built-in float")
    for notional in entry_filled_notionals:
        if type(notional) is not float or notional <= 0.0:
            raise ValueError(
                "entry_filled_notionals must contain positive built-in floats"
            )
    cost_rate = cost_bp / 10_000.0
    window_drag = sum(
        (notional / nav_usd) * cost_rate for notional in entry_filled_notionals
    )
    return 100.0 * (365.0 / window_days) * window_drag
