"""Deterministic daily Stage-B execution engine for the admitted crypto pairs.

This module is research-only.  It has no broker, account, database, scheduler,
or corpus-write surface.  It performs a candidate's full rule and its
pre-registered ablation through the same calendar-day execution implementation.
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from statistics import fmean
from typing import Literal

from .contracts import CryptoStageBRunContract
from .signals import Arm, SignalEvaluation, evaluate_signal
from .source import BoundaryAccessSpy, CryptoStageBInputError, DailyBar, DailyBarSource

__all__ = [
    "CandidatePairResult",
    "ExecutionArmResult",
    "OutcomeStatus",
    "TradeOutcome",
    "run_candidate_pair",
    "run_execution_arm",
]


OutcomeStatus = Literal[
    "capacity_rejected",
    "symbol_position_rejected",
    "entry_no_fill",
    "missing_exit",
    "delisted_exit",
    "completed",
    "censored_before_entry_boundary",
    "censored_at_exploration_boundary",
]


@dataclass(frozen=True)
class TradeOutcome:
    """A serialisable execution result for one raw signal after ranking."""

    strategy_id: str
    contract_hash: str
    arm: Arm
    venue: str
    symbol: str
    signal_session: date
    entry_session: date | None
    scheduled_exit_session: date | None
    exit_session: date | None
    status: OutcomeStatus
    rank: int
    entry_open: float | None
    exit_close: float | None
    gross_return: float | None
    net_return: float | None
    sensitivity_net_return: float | None
    delisted_exit: bool
    ranking_metrics: Mapping[str, float]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "arm": self.arm,
            "venue": self.venue,
            "symbol": self.symbol,
            "signal_session": self.signal_session.isoformat(),
            "entry_session": _date_or_none(self.entry_session),
            "scheduled_exit_session": _date_or_none(self.scheduled_exit_session),
            "exit_session": _date_or_none(self.exit_session),
            "status": self.status,
            "rank": self.rank,
            "entry_open": self.entry_open,
            "exit_close": self.exit_close,
            "gross_return": self.gross_return,
            "net_return": self.net_return,
            "sensitivity_net_return": self.sensitivity_net_return,
            "delisted_exit": self.delisted_exit,
            "ranking_metrics": dict(sorted(self.ranking_metrics.items())),
        }


@dataclass(frozen=True)
class ExecutionArmResult:
    """One full or ablation arm, with enough trace data for an independent check."""

    contract: CryptoStageBRunContract
    arm: Arm
    observations: tuple[SignalEvaluation, ...]
    outcomes: tuple[TradeOutcome, ...]
    access_summary: Mapping[str, int]

    @property
    def resolved_returns(self) -> tuple[float, ...]:
        return tuple(
            outcome.net_return
            for outcome in self.outcomes
            if outcome.net_return is not None
        )

    @property
    def net_mean_return(self) -> float | None:
        returns = self.resolved_returns
        return fmean(returns) if returns else None

    @property
    def sensitivity_net_mean_return(self) -> float | None:
        returns = tuple(
            outcome.sensitivity_net_return
            for outcome in self.outcomes
            if outcome.sensitivity_net_return is not None
        )
        return fmean(returns) if returns else None

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": "crypto-stage-b-v1",
            "strategy_id": self.contract.candidate.strategy_id,
            "contract_hash": self.contract.candidate.contract_hash,
            "config_hash": self.contract.config_hash,
            "arm": self.arm,
            "run_contract": self.contract.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "outcomes": [item.to_dict() for item in self.outcomes],
            "net_mean_return": self.net_mean_return,
            "sensitivity_net_mean_return": self.sensitivity_net_mean_return,
            "access_summary": dict(self.access_summary),
            "orders": 0,
            "account_mutations": 0,
        }


@dataclass(frozen=True)
class CandidatePairResult:
    """A candidate's full and ablation arms produced by the same engine."""

    contract: CryptoStageBRunContract
    full: ExecutionArmResult
    ablation: ExecutionArmResult

    @property
    def incremental_net_mean_return(self) -> float | None:
        if self.full.net_mean_return is None or self.ablation.net_mean_return is None:
            return None
        return self.full.net_mean_return - self.ablation.net_mean_return

    @property
    def incremental_state(self) -> str:
        if self.incremental_net_mean_return is None:
            return "INCONCLUSIVE_EMPTY_ARM"
        if self.incremental_net_mean_return > 0.0:
            return "FULL_EXCEEDS_ABLATION"
        return "FULL_DOES_NOT_EXCEED_ABLATION"

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": "crypto-stage-b-v1",
            "strategy_id": self.contract.candidate.strategy_id,
            "contract_hash": self.contract.candidate.contract_hash,
            "source_return_sha256": self.contract.candidate.source_return_sha256,
            "venue": self.contract.venue,
            "full": self.full.to_dict(),
            "ablation": self.ablation.to_dict(),
            "incremental": {
                "net_mean_return": self.incremental_net_mean_return,
                "state": self.incremental_state,
            },
        }


@dataclass(frozen=True)
class _Reservation:
    symbol: str
    entry_session: date
    occupied_through: date


def run_candidate_pair(
    *,
    source: DailyBarSource,
    contract: CryptoStageBRunContract,
) -> CandidatePairResult:
    """Run the full candidate and mandatory ablation with identical execution rules."""
    full = run_execution_arm(source=source, contract=contract, arm="full")
    ablation = run_execution_arm(source=source, contract=contract, arm="ablation")
    return CandidatePairResult(contract=contract, full=full, ablation=ablation)


def run_execution_arm(
    *,
    source: DailyBarSource,
    contract: CryptoStageBRunContract,
    arm: Arm,
) -> ExecutionArmResult:
    """Evaluate one arm without ever reading beyond the explicit UTC boundary."""
    if arm not in {"full", "ablation"}:
        raise ValueError(f"unsupported execution arm: {arm!r}")
    spy = BoundaryAccessSpy(
        source,
        exploration_start=contract.exploration_start,
        exploration_end=contract.exploration_end,
    )
    observations: list[SignalEvaluation] = []
    signals_by_session: dict[date, list[SignalEvaluation]] = defaultdict(list)
    symbols = spy.symbols(contract.venue)

    for session in _date_range(contract.exploration_start, contract.exploration_end):
        for symbol in symbols:
            observation = _evaluate_symbol_day(
                spy=spy,
                contract=contract,
                symbol=symbol,
                signal_session=session,
                arm=arm,
            )
            observations.append(observation)
            if observation.signal:
                signals_by_session[session].append(observation)

    reservations: list[_Reservation] = []
    outcomes: list[TradeOutcome] = []
    max_positions = int(
        contract.candidate.parameter("max_concurrent_positions_per_venue")
    )
    if max_positions != 5:
        raise ValueError("candidate contract drifted from venue maximum of five")

    for signal_session in sorted(signals_by_session):
        ranked = sorted(signals_by_session[signal_session], key=_ranking_key)
        entry_session = signal_session + timedelta(days=1)
        if entry_session > contract.exploration_end:
            outcomes.extend(
                _boundary_censored_outcome(observation, rank=index)
                for index, observation in enumerate(ranked, start=1)
            )
            continue

        active = tuple(
            reservation
            for reservation in reservations
            if _active_on(reservation, entry_session)
        )
        active_symbols = {reservation.symbol for reservation in active}
        available_slots = max_positions - len(active)
        if available_slots < 0:  # pragma: no cover - invariant guard
            raise RuntimeError("venue position cap invariant violated")
        selected = 0
        for rank, observation in enumerate(ranked, start=1):
            if observation.symbol in active_symbols:
                outcomes.append(
                    _unfilled_outcome(
                        observation,
                        status="symbol_position_rejected",
                        rank=rank,
                        entry_session=entry_session,
                    )
                )
                continue
            if selected >= available_slots:
                outcomes.append(
                    _unfilled_outcome(
                        observation,
                        status="capacity_rejected",
                        rank=rank,
                        entry_session=entry_session,
                    )
                )
                continue
            selected += 1
            outcome, reservation = _execute_selected_signal(
                spy=spy,
                contract=contract,
                observation=observation,
                rank=rank,
            )
            outcomes.append(outcome)
            if reservation is not None:
                reservations.append(reservation)
                active_symbols.add(reservation.symbol)

    spy.assert_no_outside_access()
    return ExecutionArmResult(
        contract=contract,
        arm=arm,
        observations=tuple(observations),
        outcomes=tuple(outcomes),
        access_summary=spy.summary(),
    )


def _evaluate_symbol_day(
    *,
    spy: BoundaryAccessSpy,
    contract: CryptoStageBRunContract,
    symbol: str,
    signal_session: date,
    arm: Arm,
) -> SignalEvaluation:
    history_start = signal_session - timedelta(
        days=contract.candidate.required_history_days - 1
    )
    if history_start < contract.exploration_start:
        return SignalEvaluation(
            strategy_id=contract.candidate.strategy_id,
            contract_hash=contract.candidate.contract_hash,
            arm=arm,
            venue=contract.venue,
            symbol=symbol,
            signal_session=signal_session,
            eligible=False,
            signal=False,
            exclusion_reason="history_before_exploration_boundary",
            stages={},
            metrics={},
        )
    history = tuple(
        _read_expected_bar(spy, contract.venue, symbol, session)
        for session in _date_range(history_start, signal_session)
    )
    evaluated = evaluate_signal(contract.candidate, history, arm=arm)
    # A missing final bar has no identity inside the signal helper.  The
    # engine restores the requested symbol-day so exclusions remain auditable.
    return replace(
        evaluated,
        venue=contract.venue,
        symbol=symbol,
        signal_session=signal_session,
    )


def _read_expected_bar(
    spy: BoundaryAccessSpy, venue: str, symbol: str, session: date
) -> DailyBar | None:
    bar = spy.get(venue, symbol, session)
    if bar is None:
        return None
    if (bar.venue, bar.symbol, bar.session) != (venue, symbol, session):
        raise CryptoStageBInputError(
            "daily source returned a bar that does not match its point-read key"
        )
    return bar


def _execute_selected_signal(
    *,
    spy: BoundaryAccessSpy,
    contract: CryptoStageBRunContract,
    observation: SignalEvaluation,
    rank: int,
) -> tuple[TradeOutcome, _Reservation | None]:
    entry_session = observation.signal_session + timedelta(days=1)
    entry_bar = _read_expected_bar(
        spy, contract.venue, observation.symbol, entry_session
    )
    if entry_bar is None or not _finite_positive(entry_bar.open):
        return (
            _unfilled_outcome(
                observation,
                status="entry_no_fill",
                rank=rank,
                entry_session=entry_session,
            ),
            None,
        )

    exit_days = int(contract.candidate.parameter("exit_D_plus_days"))
    if exit_days < 0:
        raise ValueError("exit_D_plus_days must be non-negative")
    scheduled_exit = entry_session + timedelta(days=exit_days)
    if scheduled_exit > contract.exploration_end:
        outcome = TradeOutcome(
            **_outcome_identity(observation),
            entry_session=entry_session,
            scheduled_exit_session=scheduled_exit,
            exit_session=None,
            status="censored_at_exploration_boundary",
            rank=rank,
            entry_open=entry_bar.open,
            exit_close=None,
            gross_return=None,
            net_return=None,
            sensitivity_net_return=None,
            delisted_exit=False,
            ranking_metrics=_ranking_metrics(observation),
        )
        return outcome, _Reservation(
            symbol=observation.symbol,
            entry_session=entry_session,
            occupied_through=contract.exploration_end,
        )

    exit_bar = _read_expected_bar(
        spy, contract.venue, observation.symbol, scheduled_exit
    )
    if exit_bar is not None and _finite_positive(exit_bar.close):
        gross = exit_bar.close / entry_bar.open - 1.0
        outcome = _resolved_outcome(
            observation,
            rank=rank,
            entry_session=entry_session,
            scheduled_exit=scheduled_exit,
            exit_session=scheduled_exit,
            status="completed",
            entry_open=entry_bar.open,
            exit_close=exit_bar.close,
            gross_return=gross,
            cost_round_trip_bp=contract.cost.round_trip_bp,
            sensitivity_cost_round_trip_bp=contract.cost.sensitivity_round_trip_bp,
            delisted_exit=False,
        )
        return outcome, _Reservation(
            symbol=observation.symbol,
            entry_session=entry_session,
            occupied_through=scheduled_exit,
        )

    event = _first_delisted_event(
        spy=spy,
        venue=contract.venue,
        symbol=observation.symbol,
        start=entry_session,
        end=scheduled_exit,
    )
    if event is not None:
        last_close = _last_valid_close(
            spy=spy,
            venue=contract.venue,
            symbol=observation.symbol,
            start=contract.exploration_start,
            end=event,
        )
        gross = -1.0 if last_close is None else last_close / entry_bar.open - 1.0
        outcome = _resolved_outcome(
            observation,
            rank=rank,
            entry_session=entry_session,
            scheduled_exit=scheduled_exit,
            exit_session=event,
            status="delisted_exit",
            entry_open=entry_bar.open,
            exit_close=last_close,
            gross_return=gross,
            cost_round_trip_bp=contract.cost.round_trip_bp,
            sensitivity_cost_round_trip_bp=contract.cost.sensitivity_round_trip_bp,
            delisted_exit=True,
        )
        return outcome, _Reservation(
            symbol=observation.symbol,
            entry_session=entry_session,
            occupied_through=event,
        )

    return (
        TradeOutcome(
            **_outcome_identity(observation),
            entry_session=entry_session,
            scheduled_exit_session=scheduled_exit,
            exit_session=None,
            status="missing_exit",
            rank=rank,
            entry_open=entry_bar.open,
            exit_close=None,
            gross_return=None,
            net_return=None,
            sensitivity_net_return=None,
            delisted_exit=False,
            ranking_metrics=_ranking_metrics(observation),
        ),
        _Reservation(
            symbol=observation.symbol,
            entry_session=entry_session,
            occupied_through=scheduled_exit,
        ),
    )


def _first_delisted_event(
    *,
    spy: BoundaryAccessSpy,
    venue: str,
    symbol: str,
    start: date,
    end: date,
) -> date | None:
    for session in _date_range(start, end):
        event = spy.terminal_event(venue, symbol, session)
        if event is not None:
            if event.event_type != "delisted":  # pragma: no cover - source invariant
                raise CryptoStageBInputError(
                    "terminal event is not an explicit delisting"
                )
            return event.session
    return None


def _last_valid_close(
    *,
    spy: BoundaryAccessSpy,
    venue: str,
    symbol: str,
    start: date,
    end: date,
) -> float | None:
    last_close: float | None = None
    for session in _date_range(start, end):
        bar = _read_expected_bar(spy, venue, symbol, session)
        if bar is not None and _finite_positive(bar.close):
            last_close = bar.close
    return last_close


def _resolved_outcome(
    observation: SignalEvaluation,
    *,
    rank: int,
    entry_session: date,
    scheduled_exit: date,
    exit_session: date,
    status: Literal["completed", "delisted_exit"],
    entry_open: float,
    exit_close: float | None,
    gross_return: float,
    cost_round_trip_bp: int,
    sensitivity_cost_round_trip_bp: int,
    delisted_exit: bool,
) -> TradeOutcome:
    return TradeOutcome(
        **_outcome_identity(observation),
        entry_session=entry_session,
        scheduled_exit_session=scheduled_exit,
        exit_session=exit_session,
        status=status,
        rank=rank,
        entry_open=entry_open,
        exit_close=exit_close,
        gross_return=gross_return,
        net_return=gross_return - cost_round_trip_bp / 10_000.0,
        sensitivity_net_return=(
            gross_return - sensitivity_cost_round_trip_bp / 10_000.0
        ),
        delisted_exit=delisted_exit,
        ranking_metrics=_ranking_metrics(observation),
    )


def _unfilled_outcome(
    observation: SignalEvaluation,
    *,
    status: Literal["capacity_rejected", "symbol_position_rejected", "entry_no_fill"],
    rank: int,
    entry_session: date,
) -> TradeOutcome:
    return TradeOutcome(
        **_outcome_identity(observation),
        entry_session=entry_session,
        scheduled_exit_session=None,
        exit_session=None,
        status=status,
        rank=rank,
        entry_open=None,
        exit_close=None,
        gross_return=None,
        net_return=None,
        sensitivity_net_return=None,
        delisted_exit=False,
        ranking_metrics=_ranking_metrics(observation),
    )


def _boundary_censored_outcome(
    observation: SignalEvaluation, *, rank: int
) -> TradeOutcome:
    return TradeOutcome(
        **_outcome_identity(observation),
        entry_session=None,
        scheduled_exit_session=None,
        exit_session=None,
        status="censored_before_entry_boundary",
        rank=rank,
        entry_open=None,
        exit_close=None,
        gross_return=None,
        net_return=None,
        sensitivity_net_return=None,
        delisted_exit=False,
        ranking_metrics=_ranking_metrics(observation),
    )


def _outcome_identity(observation: SignalEvaluation) -> dict[str, object]:
    return {
        "strategy_id": observation.strategy_id,
        "contract_hash": observation.contract_hash,
        "arm": observation.arm,
        "venue": observation.venue,
        "symbol": observation.symbol,
        "signal_session": observation.signal_session,
    }


def _ranking_metrics(observation: SignalEvaluation) -> dict[str, float]:
    return dict(sorted(observation.metrics.items()))


def _ranking_key(observation: SignalEvaluation) -> tuple[float | str, ...]:
    """Apply the source candidate's ranking only after local eligibility/signal."""

    def metric(name: str) -> float:
        return _ranking_value(observation.metrics.get(name))

    strategy_id = observation.strategy_id
    if strategy_id == "CR-SPOT-ETR-01":
        return (
            -metric("qv_ratio"),
            -metric("clv"),
            -metric("tail_severity"),
            observation.symbol,
        )
    if strategy_id == "CR-SPOT-TPR-01":
        return (
            -metric("pullback_extension"),
            -metric("trend_slope"),
            -metric("qv_ratio"),
            observation.symbol,
        )
    if strategy_id == "CR-SPOT-CEB-01":
        return (
            -metric("qv_ratio"),
            -metric("range_ratio"),
            -metric("breakout_extension"),
            observation.symbol,
        )
    raise ValueError(f"no ranking implementation for {strategy_id!r}")


def _ranking_value(value: float | None) -> float:
    """Missing ablation-only ranking inputs sort last without changing eligibility."""
    return value if value is not None and math.isfinite(value) else -math.inf


def _active_on(reservation: _Reservation, entry_session: date) -> bool:
    """An exit-at-close position occupies its slot through that UTC calendar day."""
    return reservation.entry_session <= entry_session <= reservation.occupied_through


def _finite_positive(value: float) -> bool:
    return math.isfinite(value) and value > 0.0


def _date_range(start: date, end: date) -> Iterable[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _date_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
