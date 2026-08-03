"""Baseline pipeline smoke: value_rank_topN_D5.

JOB_PURPOSE=BACKTEST_HARNESS_WIRING_ONLY
LABEL=PIPELINE_SMOKE_NOT_A_STRATEGY

This is a **pipeline integrity** probe, not a strategy:
* each session ``t``: rank investable universe by that day's ``trading_value``
* buy top-N equal-weight
* exit at session ``t + holding_days`` (default 5)
* delisted holdings force-exit with explicit terminal events

**Fill / pricing assumption (same-bar, documented — not a strategy claim):**
entry and exit lots are marked at session ``t`` **close** on the decision
session itself (same-bar close fill). This is **not** a next-open (t+1 open)
model. Changing that assumption is a contract change, not a silent tweak.

Do **not** interpret returns, Sharpe, or rank quality. Numbers exist only to
prove the wiring moves data end-to-end without holdout / lookahead / silent
delist drops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from holdout_guard import assert_date_not_holdout, assert_range_not_holdout
from membership import MembershipRow, universe_at
from pit import Bar, LookaheadViolation, assert_no_lookahead, bars_available_at
from terminal_events import TerminalEvent, force_exit_delisted_holdings
from windows import EXPLORATION_WINDOW, parse_iso_date

__all__ = [
    "PIPELINE_SMOKE_LABEL",
    "BASELINE_NAME",
    "OpenLot",
    "SmokeResult",
    "run_value_rank_topn_d5",
]

PIPELINE_SMOKE_LABEL = "PIPELINE_SMOKE_NOT_A_STRATEGY"
BASELINE_NAME = "value_rank_topN_D5"


@dataclass
class OpenLot:
    symbol: str
    entry_session: date
    entry_price: float
    weight: float
    exit_session_index: int


@dataclass
class SmokeResult:
    label: str = PIPELINE_SMOKE_LABEL
    baseline: str = BASELINE_NAME
    job_purpose: str = "BACKTEST_HARNESS_WIRING_ONLY"
    schema_is_inferred_from_literals: bool = True
    sessions_processed: int = 0
    entries: int = 0
    exits: int = 0
    terminal_delisted: int = 0
    terminal_events: list[TerminalEvent] = field(default_factory=list)
    closed_gross_sum: float = 0.0  # raw sum of lot returns; NOT for interpretation
    notes: list[str] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "baseline": self.baseline,
            "job_purpose": self.job_purpose,
            "schema_is_inferred_from_literals": self.schema_is_inferred_from_literals,
            "PIPELINE_SMOKE_NOT_A_STRATEGY": True,
            "sessions_processed": self.sessions_processed,
            "entries": self.entries,
            "exits": self.exits,
            "terminal_delisted": self.terminal_delisted,
            "closed_gross_sum": self.closed_gross_sum,
            "notes": list(self.notes),
            "interpretation": "FORBIDDEN — pipeline smoke only",
        }


def run_value_rank_topn_d5(
    *,
    bars: list[Bar],
    membership: list[MembershipRow],
    top_n: int = 3,
    holding_days: int = 5,
    window_start: date | str = EXPLORATION_WINDOW.start,
    window_end: date | str = EXPLORATION_WINDOW.end,
) -> SmokeResult:
    """Run the baseline over an exploration window only (holdout refused)."""
    start, end = assert_range_not_holdout(window_start, window_end)
    if top_n < 1:
        raise ValueError("top_n must be >= 1")
    if holding_days < 1:
        raise ValueError("holding_days must be >= 1")

    result = SmokeResult()
    result.notes.append(
        "PIPELINE_SMOKE_NOT_A_STRATEGY — do not interpret closed_gross_sum"
    )

    # Ordered unique sessions present in bars within window.
    sessions = sorted({b.session_date for b in bars if start <= b.session_date <= end})
    # Index for D+N exit scheduling.
    session_index = {s: i for i, s in enumerate(sessions)}

    open_lots: list[OpenLot] = []
    held: set[str] = set()

    for s in sessions:
        assert_date_not_holdout(s)
        result.sessions_processed += 1

        # PIT bars for ranking / pricing at s.
        pit_bars = bars_available_at(bars, s)
        assert_no_lookahead(pit_bars, s)

        # Day-s bars only for ranking (need trading_value at s).
        day_bars = [b for b in pit_bars if b.session_date == s]
        close_by_symbol = {b.symbol: b.close for b in day_bars}
        value_by_symbol = {b.symbol: b.trading_value for b in day_bars}

        snap = universe_at(membership, s)

        # Explicit delist terminalization (never silent drop).
        held, term_events = force_exit_delisted_holdings(
            session_date=s,
            held_symbols=held,
            delisted_as_of=snap.delisted_as_of,
            last_close_by_symbol=close_by_symbol,
        )
        for ev in term_events:
            result.terminal_events.append(ev)
            result.terminal_delisted += 1
            # Close matching open lots at last close (0 if missing).
            remaining: list[OpenLot] = []
            for lot in open_lots:
                if lot.symbol == ev.symbol:
                    px = ev.last_close if ev.last_close is not None else lot.entry_price
                    result.closed_gross_sum += (px / lot.entry_price - 1.0) * lot.weight
                    result.exits += 1
                    held.discard(lot.symbol)
                else:
                    remaining.append(lot)
            open_lots = remaining

        # Scheduled D+N exits.
        still_open: list[OpenLot] = []
        si = session_index[s]
        for lot in open_lots:
            if si >= lot.exit_session_index:
                if lot.symbol not in close_by_symbol:
                    # No price today — keep open (fixture should avoid this).
                    still_open.append(lot)
                    continue
                px = close_by_symbol[lot.symbol]
                # Lookahead guard: exit price bar date must be <= s (it is s).
                exit_bar = next(b for b in day_bars if b.symbol == lot.symbol)
                assert_no_lookahead([exit_bar], s)
                result.closed_gross_sum += (px / lot.entry_price - 1.0) * lot.weight
                result.exits += 1
                held.discard(lot.symbol)
            else:
                still_open.append(lot)
        open_lots = still_open

        # Entries: top-N by trading_value among investable with a bar today.
        candidates = [
            sym for sym in snap.symbols if sym in value_by_symbol and sym not in held
        ]
        candidates.sort(key=lambda sym: value_by_symbol[sym], reverse=True)
        picks = candidates[:top_n]
        if not picks:
            continue
        weight = 1.0 / len(picks)
        exit_idx = si + holding_days
        for sym in picks:
            entry_bar = next(b for b in day_bars if b.symbol == sym)
            # Decision at s uses only bars <= s — re-assert.
            used = bars_available_at(bars, s, symbol=sym)
            assert_no_lookahead(used, s)
            if entry_bar.session_date != s:
                raise LookaheadViolation(
                    f"entry bar date {entry_bar.session_date} != decision {s}"
                )
            open_lots.append(
                OpenLot(
                    symbol=sym,
                    entry_session=s,
                    entry_price=entry_bar.close,
                    weight=weight,
                    exit_session_index=exit_idx,
                )
            )
            held.add(sym)
            result.entries += 1

    result.notes.append(
        f"window={start.isoformat()}..{end.isoformat()} top_n={top_n} "
        f"holding_days={holding_days}"
    )
    return result


def parse_window_bound(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return parse_iso_date(value)
