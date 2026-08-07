"""B0 replay engine for the calibration window.

``PortfolioEngine._signal_for_session`` recomputes Wilder RSI over a symbol's
whole contiguous history on every decision bar, which is quadratic and cannot
finish a corpus-scale replay. ``primary.PrimaryPortfolioEngine`` solves this
with a precomputed signal tape, but its ``__init__`` hard-codes the deny-list
``SealedAccessGuard`` and A2 forbids editing that file. This class therefore
repeats the same two overrides against the shared, unmodified tape builder
(``primary_corpus._prepare_signal_tape``) while accepting an injected guard.

The tape is exact, not an approximation: ``bollinger_bands`` reads only the
last 20 closes and ``scan_fib_window`` only the prior 120, so a 120-bar history
slice reproduces the base engine's fib/Bollinger inputs bit-for-bit, and the
tape's running Wilder state reproduces its RSI over the full contiguous
segment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from research.kr_corpus.d3_engine.calibration_guard import CalibrationAccessGuard
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.models import (
    EngineResult,
    PortfolioRunInput,
    RunState,
)
from research.kr_corpus.d3_engine.primary_corpus import SignalSnapshot
from research.kr_corpus.d3_engine.tick import TickTable


@dataclass(slots=True)
class CalibrationTrace:
    """Daily capital-share observations, at the contract's locked-share grain."""

    daily: list[dict[str, object]] = field(default_factory=list)
    engine_invocations: int = 0


class CalibrationPortfolioEngine(PortfolioEngine):
    """E1 execution core over the calibration window with an injected guard."""

    def __init__(
        self,
        tick_table: TickTable,
        *,
        access_guard: CalibrationAccessGuard,
        signals: dict[tuple[date, str], SignalSnapshot],
    ) -> None:
        super().__init__(tick_table, access_guard=access_guard)
        self._signals = signals
        self._trace = CalibrationTrace()
        self._capture = False

    def execute(
        self, run_input: PortfolioRunInput
    ) -> tuple[EngineResult, CalibrationTrace]:
        self._trace = CalibrationTrace()
        self._capture = True
        try:
            self._trace.engine_invocations += 1
            result = super()._run(run_input)
        finally:
            self._capture = False
        return result, self._trace

    @staticmethod
    def _signal_history(
        symbol_bars: list[Any], *, index: int, segment_start: int
    ) -> list[Any]:
        """The tape only needs t and its prior 120-bar association."""

        return symbol_bars[max(segment_start, index - 120) : index + 1]

    def _signal_for_session(
        self, history: list[Any], decision_index: int
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        current = history[decision_index]
        snapshot = self._signals.get((current.session, current.symbol))
        if snapshot is None:
            return None
        return (
            snapshot.rsi,
            snapshot.l2_price,
            snapshot.fib_high,
            snapshot.fib_low,
        )

    def _finish_day(
        self,
        *,
        state: RunState,
        cash: Any,
        by_session: dict[tuple[date, str], Any],
        session: date,
        cumulative_contribution: Decimal,
        cumulative_contribution_series: list[Decimal],
        daily_invested: list[Decimal],
        daily_locked_ratios: list[Decimal],
        nav_series: list[Decimal],
        last_closes: dict[str, Decimal],
        fee_rate: Decimal,
        terminal: bool,
    ) -> None:
        PortfolioEngine._finish_day(
            state=state,
            cash=cash,
            by_session=by_session,
            session=session,
            cumulative_contribution=cumulative_contribution,
            cumulative_contribution_series=cumulative_contribution_series,
            daily_invested=daily_invested,
            daily_locked_ratios=daily_locked_ratios,
            nav_series=nav_series,
            last_closes=last_closes,
            fee_rate=fee_rate,
            terminal=terminal,
        )
        if not self._capture:
            return
        self._trace.daily.append(
            {
                "session": session,
                "invested_cost_basis": daily_invested[-1],
                "locked_cost_basis": daily_locked_ratios[-1] * daily_invested[-1],
                "locked_share": daily_locked_ratios[-1],
                "open_positions": sum(
                    1 for position in state.positions.values() if position.quantity > 0
                ),
                "nav": nav_series[-1],
            }
        )
