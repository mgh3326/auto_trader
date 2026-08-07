"""Additive locked-clock correction golden cases and executable mutants.

The immutable external D3 golden remains 33/33.  These synthetic cases exercise
the portfolio engine itself and cover the arm-scope hole found after D3-R1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.models import (
    Arm,
    Bar,
    CashflowView,
    EngineResult,
    Order,
    OrderClass,
    PortfolioRunInput,
    Position,
)
from research.kr_corpus.d3_engine.policies import UnderwaterClockOutcome
from research.kr_corpus.d3_engine.tick import TickTable

_SYMBOL = "005930"
_PRE_ENTRY_SESSIONS = 200
_UNDERWATER_SESSIONS = 220
_EXPECTED_LOCKED_MEAN = Decimal("0.18636363636363636363636363636363636363636363636364")


@dataclass(frozen=True, slots=True)
class CorrectionGoldenCase:
    name: str
    arm: Arm
    passed: bool
    expected: dict[str, object]
    actual: dict[str, object]


@dataclass(frozen=True, slots=True)
class CorrectionMutantProbe:
    name: str
    differs: bool
    correct: dict[str, object]
    mutant: dict[str, object]


class _ClockSyntheticEngine(PortfolioEngine):
    """Natural fill path with all unrelated signal/sell behavior held fixed."""

    def __init__(self, tick_table: TickTable, *, entry_session: date) -> None:
        super().__init__(tick_table)
        self._entry_session = entry_session

    def _signal_for_session(
        self, history: list[Bar], decision_index: int
    ) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
        if history[decision_index].session != self._entry_session:
            return None
        return (
            Decimal("40"),
            Decimal("9500"),
            Decimal("12000"),
            Decimal("8000"),
        )

    def _resistance_orders(
        self,
        *,
        symbol: str,
        session: date,
        history: list[Bar],
        index: int,
        position: Position,
        first_order_number: int,
    ) -> list[Order]:
        del symbol, session, history, index, position, first_order_number
        return []


class _C3OnlyClockMutant(_ClockSyntheticEngine):
    @staticmethod
    def _underwater_clock_enabled(arm: Arm) -> bool:
        return arm is Arm.C3


class _MissingCloseCarryMutant(_ClockSyntheticEngine):
    @staticmethod
    def _reset_metric_close(position: Position) -> None:
        del position


class _B0TrimFiresMutant(_ClockSyntheticEngine):
    @staticmethod
    def _c3_time_trim_policy_enabled(arm: Arm) -> bool:
        return arm in {Arm.B0, Arm.C3}


class _EqualityCarryMutant(_ClockSyntheticEngine):
    @staticmethod
    def _update_metric_close(
        position: Position, *, close: Decimal
    ) -> UnderwaterClockOutcome:
        if position.quantity == 0:
            position.underwater_streak = 0
            return UnderwaterClockOutcome(False, 0)
        if close < position.average_price:
            position.underwater_streak += 1
            return UnderwaterClockOutcome(True, position.underwater_streak)
        return UnderwaterClockOutcome(False, position.underwater_streak)


def run_correction_golden(
    tick_table: TickTable,
) -> tuple[CorrectionGoldenCase, ...]:
    expected = {
        "fills": 2,
        "terminal_underwater_streak": _UNDERWATER_SESSIONS,
        "locked_share_tw_mean": _EXPECTED_LOCKED_MEAN,
        "locked_share_p95": Decimal(1),
        "locked_share_max": Decimal(1),
        "time_trim_fills": 0,
    }
    cases: list[CorrectionGoldenCase] = []
    for arm in (Arm.B0, Arm.C1, Arm.C2):
        result = _run_synthetic(tick_table, arm=arm)
        actual = _projection(result)
        cases.append(
            CorrectionGoldenCase(
                name=f"{arm.value}_180_plus_underwater_locked_nonzero",
                arm=arm,
                passed=actual == expected,
                expected=dict(expected),
                actual=actual,
            )
        )
    return tuple(cases)


def run_correction_mutants(
    tick_table: TickTable,
) -> tuple[CorrectionMutantProbe, ...]:
    correct_scope = _projection(_run_synthetic(tick_table, arm=Arm.B0))
    mutant_scope = _projection(
        _run_synthetic(tick_table, arm=Arm.B0, engine_type=_C3OnlyClockMutant)
    )

    correct_missing = _projection(
        _run_synthetic(tick_table, arm=Arm.B0, missing_offset=100)
    )
    mutant_missing = _projection(
        _run_synthetic(
            tick_table,
            arm=Arm.B0,
            missing_offset=100,
            engine_type=_MissingCloseCarryMutant,
        )
    )

    correct_trim = _projection(_run_synthetic(tick_table, arm=Arm.B0))
    mutant_trim = _projection(
        _run_synthetic(tick_table, arm=Arm.B0, engine_type=_B0TrimFiresMutant)
    )

    correct_equality = _projection(
        _run_synthetic(tick_table, arm=Arm.B0, equality_offset=100)
    )
    mutant_equality = _projection(
        _run_synthetic(
            tick_table,
            arm=Arm.B0,
            equality_offset=100,
            engine_type=_EqualityCarryMutant,
        )
    )

    return (
        CorrectionMutantProbe(
            "C3-only-clock", correct_scope != mutant_scope, correct_scope, mutant_scope
        ),
        CorrectionMutantProbe(
            "missing-close-reset",
            correct_missing != mutant_missing,
            correct_missing,
            mutant_missing,
        ),
        CorrectionMutantProbe(
            "B0-trim-fires", correct_trim != mutant_trim, correct_trim, mutant_trim
        ),
        CorrectionMutantProbe(
            "close-ge-average-keeps-streak",
            correct_equality != mutant_equality,
            correct_equality,
            mutant_equality,
        ),
    )


def _run_synthetic(
    tick_table: TickTable,
    *,
    arm: Arm,
    missing_offset: int | None = None,
    equality_offset: int | None = None,
    engine_type: type[_ClockSyntheticEngine] = _ClockSyntheticEngine,
) -> EngineResult:
    total = _PRE_ENTRY_SESSIONS + _UNDERWATER_SESSIONS
    sessions = tuple(date(2014, 1, 1) + timedelta(days=index) for index in range(total))
    entry_session = sessions[_PRE_ENTRY_SESSIONS]
    bars: list[Bar] = []
    for index, session in enumerate(sessions):
        relative = index - _PRE_ENTRY_SESSIONS
        if relative == missing_offset:
            continue
        if relative < 0:
            open_price = high = close = Decimal("10000")
            low = Decimal("9900")
        elif relative == 0:
            open_price = Decimal("9400")
            high = Decimal("10000")
            low = close = Decimal("9000")
        elif relative == equality_offset:
            open_price = Decimal("9000")
            high = close = Decimal("9400")
            low = Decimal("8900")
        else:
            open_price = close = Decimal("9000")
            high = Decimal("9100")
            low = Decimal("8900")
        bars.append(
            Bar(
                session=session,
                symbol=_SYMBOL,
                open=open_price,
                high=high,
                low=low,
                close=close,
            )
        )
    engine = engine_type(tick_table, entry_session=entry_session)
    return engine.run(
        PortfolioRunInput(
            arm=arm,
            cashflow_view=CashflowView.NO_CONTRIBUTION,
            bars=tuple(bars),
            market_sessions=sessions,
            index_closes=tuple((session, Decimal("2000")) for session in sessions),
            decision_start=entry_session,
        )
    )


def _projection(result: EngineResult) -> dict[str, object]:
    terminal = result.terminal_positions
    if len(terminal) != 1:
        raise AssertionError(f"synthetic correction terminal lot drift:{len(terminal)}")
    return {
        "fills": len(result.fills),
        "terminal_underwater_streak": terminal[0]["underwater_streak"],
        "locked_share_tw_mean": result.metrics["locked_share_tw_mean"],
        "locked_share_p95": result.metrics["locked_share_p95"],
        "locked_share_max": result.metrics["locked_share_max"],
        "time_trim_fills": sum(
            fill.order_class is OrderClass.TIME_TRIM for fill in result.fills
        ),
    }


def correction_acceptance_payload(tick_table: TickTable) -> dict[str, Any]:
    golden = run_correction_golden(tick_table)
    mutants = run_correction_mutants(tick_table)
    return {
        "golden": {
            "passed": sum(case.passed for case in golden),
            "total": len(golden),
            "cases": [
                {
                    "name": case.name,
                    "arm": case.arm,
                    "passed": case.passed,
                    "expected": case.expected,
                    "actual": case.actual,
                }
                for case in golden
            ],
        },
        "mutants": {
            "passed": sum(probe.differs for probe in mutants),
            "total": len(mutants),
            "cases": [
                {
                    "name": probe.name,
                    "differs": probe.differs,
                    "correct": probe.correct,
                    "mutant": probe.mutant,
                }
                for probe in mutants
            ],
        },
    }
