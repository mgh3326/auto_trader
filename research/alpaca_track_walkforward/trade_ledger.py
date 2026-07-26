"""ROB-1062 H4 — turn one (symbol, config_id) chronological stream of H3
``output_schema.SignalRecord``s into CLOSED trades, applying the historical
fill model (``fill_model.py``) to both legs.

``SignalRecord`` itself carries no price (H3 is PnL-blind by construction,
AC20/AC21 of ROB-1061) — only ``action``/``reason_code``/``evidence_hash``.
This module is the FIRST point in the pipeline where a price ever touches a
record, and it does so from data the CALLER supplies independently (the
reference close at each decision, and the minute bars following it) — never
by reaching back into a signal record's evidence_hash (which is an opaque
digest, not a reversible price).

AP-A1/AP-A2 are both single-position-per-symbol (never pyramided, AC9/H3):
an ``ENTER`` record is matched with the NEXT ``EXIT`` record for the same
``(symbol, config_id)`` that follows it — there is never more than one open
leg per symbol/config at a time. An ``ENTER`` with no following ``EXIT`` by
the end of the supplied stream is an OPEN position (not a closed trade) and
is reported separately, never silently counted as closed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import fill_model as fm
from daily_bars import SpotMinute
from output_schema import SignalRecord

__all__ = [
    "FillAttempt",
    "OpenLeg",
    "Trade",
    "TradeLedgerResult",
    "build_trades_for_symbol_config",
    "process_entry_signal",
    "process_exit_signal",
]


@dataclass(frozen=True)
class FillAttempt:
    """One ENTRY or EXIT fill attempt, with its own decision timestamp —
    exists so a caller can attribute an unfilled/incomplete attempt to a
    walk-forward PHASE (TRAIN vs OOS) by timestamp, without needing to
    re-run the ledger over a phase-sliced (and therefore potentially
    ENTER/EXIT-orphaning) record subsequence. A single canonical ledger run
    over the FULL continuous stream is the only ledger run that ever
    happens; every consumer downstream (blind counts, trade attribution)
    filters THIS list by timestamp instead."""

    decision_ts_ms: int
    symbol: str
    leg: str  # "ENTRY" or "EXIT"
    outcome: fm.FillOutcome


@dataclass(frozen=True)
class OpenLeg:
    """An ENTER with no matching EXIT yet inside the supplied record
    stream — genuinely open, never counted as a closed trade."""

    symbol: str
    config_id: str
    entry_decision_ts_ms: int
    entry_reference_close: float
    entry_fill: fm.FillOutcome


@dataclass(frozen=True)
class Trade:
    """One CLOSED round trip: an ENTER matched with the following EXIT for
    the same symbol/config, both legs run through the fill model."""

    symbol: str
    config_id: str
    entry_decision_ts_ms: int
    exit_decision_ts_ms: int
    entry_reference_close: float
    exit_reference_close: float
    entry_fill: fm.FillOutcome
    exit_fill: fm.FillOutcome

    def __post_init__(self) -> None:
        if self.exit_decision_ts_ms <= self.entry_decision_ts_ms:
            raise ValueError("exit must come strictly after entry")

    @property
    def both_legs_filled(self) -> bool:
        return self.entry_fill.filled and self.exit_fill.filled

    @property
    def holding_days(self) -> int:
        return (self.exit_decision_ts_ms - self.entry_decision_ts_ms) // 86_400_000


@dataclass(frozen=True)
class TradeLedgerResult:
    closed_trades: tuple[Trade, ...]
    open_position: OpenLeg | None
    entry_unfilled_count: int
    exit_unfilled_count: int
    fill_window_incomplete_count: int
    fill_attempts: tuple[FillAttempt, ...] = ()


def process_entry_signal(
    record: SignalRecord,
    *,
    reference_close: float,
    minute_bars: Sequence[SpotMinute],
) -> tuple[FillAttempt, OpenLeg | None]:
    """The single source of truth for turning one ENTER record into a fill
    attempt + (maybe) a new open leg. Shared by ``build_trades_for_symbol_
    config`` (a standalone, already-consistent-stream batch pass) and the
    walk-forward runner's interleaved per-decision loop (which additionally
    needs to know THIS RESULT immediately, in order to patch H3's engine
    state before the next decision — see ``runner.py`` module docstring for
    why: H3's own position bookkeeping assumes every accepted signal
    executes instantly, which the fill model can contradict)."""
    ts = record.decision_ts_ms
    fill = fm.model_entry_fill(
        decision_ts_ms=ts,
        reference_close=reference_close,
        minute_bars_after_signal=minute_bars,
    )
    attempt = FillAttempt(
        decision_ts_ms=ts, symbol=record.symbol, leg="ENTRY", outcome=fill
    )
    if not fill.filled:
        return attempt, None
    return attempt, OpenLeg(
        symbol=record.symbol,
        config_id=record.config_id,
        entry_decision_ts_ms=ts,
        entry_reference_close=reference_close,
        entry_fill=fill,
    )


def process_exit_signal(
    record: SignalRecord,
    open_leg: OpenLeg,
    *,
    reference_close: float,
    minute_bars: Sequence[SpotMinute],
) -> tuple[FillAttempt, Trade | None]:
    """The single source of truth for turning one EXIT record + its known
    open leg into a fill attempt + (maybe) a closed Trade. Returns
    ``(attempt, None)`` if the exit did not fill (unfilled or incomplete) —
    the caller keeps treating ``open_leg`` as still open."""
    ts = record.decision_ts_ms
    fill = fm.model_exit_fill(
        decision_ts_ms=ts,
        reference_close=reference_close,
        minute_bars_after_signal=minute_bars,
    )
    attempt = FillAttempt(
        decision_ts_ms=ts, symbol=record.symbol, leg="EXIT", outcome=fill
    )
    if not fill.filled:
        return attempt, None
    trade = Trade(
        symbol=open_leg.symbol,
        config_id=open_leg.config_id,
        entry_decision_ts_ms=open_leg.entry_decision_ts_ms,
        exit_decision_ts_ms=ts,
        entry_reference_close=open_leg.entry_reference_close,
        exit_reference_close=reference_close,
        entry_fill=open_leg.entry_fill,
        exit_fill=fill,
    )
    return attempt, trade


def build_trades_for_symbol_config(
    records: Sequence[SignalRecord],
    *,
    reference_close_by_decision_ts: Mapping[int, float],
    minute_bars_by_decision_ts: Mapping[int, Sequence[SpotMinute]],
) -> TradeLedgerResult:
    """``records`` must already be the chronological, single-(symbol,
    config_id) slice (the caller is the walk-forward runner, which knows
    the symbol/config grouping — this function does not re-derive it, to
    stay a small, directly testable unit)."""
    if records:
        symbol = records[0].symbol
        config_id = records[0].config_id
        for r in records:
            if r.symbol != symbol or r.config_id != config_id:
                raise ValueError(
                    "records must all share the same (symbol, config_id) — "
                    f"got {(r.symbol, r.config_id)!r} mixed with "
                    f"{(symbol, config_id)!r}"
                )
        for earlier, later in zip(records, records[1:], strict=False):
            if later.decision_ts_ms <= earlier.decision_ts_ms:
                raise ValueError(
                    "records must be strictly increasing by decision_ts_ms"
                )

    closed: list[Trade] = []
    open_leg: OpenLeg | None = None
    entry_unfilled = 0
    exit_unfilled = 0
    incomplete = 0
    attempts: list[FillAttempt] = []

    for record in records:
        ts = record.decision_ts_ms
        if record.action == "ENTER":
            ref = reference_close_by_decision_ts[ts]
            bars = minute_bars_by_decision_ts.get(ts, ())
            attempt, maybe_open_leg = process_entry_signal(
                record, reference_close=ref, minute_bars=bars
            )
            attempts.append(attempt)
            if attempt.outcome.reason == "FILL_WINDOW_INCOMPLETE":
                incomplete += 1
                continue
            if maybe_open_leg is None:
                entry_unfilled += 1
                continue
            open_leg = maybe_open_leg
        elif record.action == "EXIT":
            if open_leg is None:
                # H3 never emits an EXIT for a symbol it did not itself
                # carry as long (state is caller-owned and continuous) —
                # an EXIT with no open leg here means the caller fed a
                # stream that does not start from a flat/known state.
                raise ValueError(
                    f"EXIT at {ts} for {record.symbol}/{record.config_id} with "
                    "no matching open ENTER in this record stream"
                )
            ref = reference_close_by_decision_ts[ts]
            bars = minute_bars_by_decision_ts.get(ts, ())
            attempt, maybe_trade = process_exit_signal(
                record, open_leg, reference_close=ref, minute_bars=bars
            )
            attempts.append(attempt)
            if attempt.outcome.reason == "FILL_WINDOW_INCOMPLETE":
                incomplete += 1
                # The open leg survives — an incomplete OOS fill attempt on
                # the exit leg is a structural data gap, not an exit event;
                # the position is still open from this ledger's perspective.
                continue
            if maybe_trade is None:
                exit_unfilled += 1
                # EXIT_UNFILLED: the position remains open (SS13.2-style
                # retry-until-filled economics are the live loop's scope;
                # this backtest ledger simply keeps the leg open for the
                # next EXIT-shaped record to attempt again).
                continue
            closed.append(maybe_trade)
            open_leg = None
        # HOLD/NO_ACTION records do not affect the ledger.

    return TradeLedgerResult(
        closed_trades=tuple(closed),
        open_position=open_leg,
        entry_unfilled_count=entry_unfilled,
        exit_unfilled_count=exit_unfilled,
        fill_window_incomplete_count=incomplete,
        fill_attempts=tuple(attempts),
    )
