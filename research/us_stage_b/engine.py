"""Deterministic US Stage-B execution and cohort-comparison engine.

This module is intentionally isolated from KR, crypto, brokers, accounts,
databases, schedulers, and shared signal implementations.  It consumes an
explicit corpus-session index, so every entry and D+N maturity is index-based
rather than inferred from calendar dates.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from statistics import mean
from typing import TYPE_CHECKING, Any, Literal

from .contracts import USStageBRunContract
from .signals import SignalObservation, evaluate_signal
from .source import (
    ExplorationBoundaryAccessSpy,
    USBarSource,
    USStageBDailyBar,
    USStageBInputError,
)

if TYPE_CHECKING:
    from .verdict import FalsificationVerdict, RevCostProfileVerdicts

__all__ = [
    "CohortComparison",
    "TradeOutcome",
    "USStageBRunResult",
    "USStageBRunError",
    "liquidity_decile_assignments",
    "rank_signal_observations",
    "run_us_stage_b",
]


class USStageBRunError(RuntimeError):
    """A deterministic run input or invariant cannot be trusted."""


OutcomeStatus = Literal[
    "completed",
    "entry_no_fill",
    "capacity_rejected",
    "run_invalid_missing_exit",
]


@dataclass(frozen=True)
class TradeOutcome:
    """One post-signal decision, including all non-filled outcomes."""

    strategy_id: str
    contract_hash: str
    labels: tuple[str, ...]
    symbol: str
    signal_session: date
    entry_session: date | None
    exit_session: date | None
    status: OutcomeStatus
    selection_rank: int | None
    fixed_notional_usd: float
    adv20_pre_proxy: float
    tie_break_sha256: str
    entry_open: float | None
    exit_adjusted_close: float | None
    gross_return: float | None
    base_net_return: float | None
    sensitivity_net_return: float | None
    volume_ratio20: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "symbol": self.symbol,
            "signal_session": self.signal_session.isoformat(),
            "entry_session": _date_or_none(self.entry_session),
            "exit_session": _date_or_none(self.exit_session),
            "status": self.status,
            "selection_rank": self.selection_rank,
            "fixed_notional_usd": self.fixed_notional_usd,
            "adv20_pre_proxy": self.adv20_pre_proxy,
            "tie_break_sha256": self.tie_break_sha256,
            "entry_open": self.entry_open,
            "exit_adjusted_close": self.exit_adjusted_close,
            "gross_return": self.gross_return,
            "base_net_return": self.base_net_return,
            "sensitivity_net_return": self.sensitivity_net_return,
            "volume_ratio20": self.volume_ratio20,
        }


@dataclass(frozen=True)
class CohortComparison:
    """One leave-one-out, same-session, same-ADV-decile comparison artifact."""

    strategy_id: str
    contract_hash: str
    labels: tuple[str, ...]
    symbol: str
    signal_session: date
    entry_session: date
    exit_session: date
    liquidity_decile: int
    eligible_universe_size: int
    leave_one_out_member_count: int
    excluded_entry_no_fill_count: int
    excluded_maturity_close_count: int
    status: Literal["completed", "cohort_unavailable"]
    candidate_base_net_return: float
    candidate_sensitivity_net_return: float
    baseline_base_net_return: float | None
    baseline_sensitivity_net_return: float | None
    base_excess_return: float | None
    sensitivity_excess_return: float | None
    volume_ratio20: float | None
    tie_break_sha256: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "symbol": self.symbol,
            "signal_session": self.signal_session.isoformat(),
            "entry_session": self.entry_session.isoformat(),
            "exit_session": self.exit_session.isoformat(),
            "liquidity_decile": self.liquidity_decile,
            "decile_convention": "self_inclusive_adv_percentile_equal_values",
            "eligible_universe_size": self.eligible_universe_size,
            "leave_one_out_member_count": self.leave_one_out_member_count,
            "excluded_entry_no_fill_count": self.excluded_entry_no_fill_count,
            "excluded_maturity_close_count": self.excluded_maturity_close_count,
            "status": self.status,
            "candidate_base_net_return": self.candidate_base_net_return,
            "candidate_sensitivity_net_return": self.candidate_sensitivity_net_return,
            "baseline_base_net_return": self.baseline_base_net_return,
            "baseline_sensitivity_net_return": self.baseline_sensitivity_net_return,
            "base_excess_return": self.base_excess_return,
            "sensitivity_excess_return": self.sensitivity_excess_return,
            "volume_ratio20": self.volume_ratio20,
            "tie_break_sha256": self.tie_break_sha256,
        }


@dataclass(frozen=True)
class USStageBRunResult:
    """Serializable full run state; an empty result remains explicitly labeled."""

    contract: USStageBRunContract
    corpus_sessions: tuple[date, ...]
    observations: tuple[SignalObservation, ...]
    outcomes: tuple[TradeOutcome, ...]
    cohorts: tuple[CohortComparison, ...]
    run_invalid: bool
    invalid_reasons: tuple[str, ...]
    access_summary: Mapping[str, int]
    verdict: FalsificationVerdict
    cost_profile_verdicts: RevCostProfileVerdicts | None

    def __post_init__(self) -> None:
        is_rev = self.strategy_id == "US-TS-REV-SHORT-Z3-T126-H3-v1"
        if is_rev != (self.cost_profile_verdicts is not None):
            raise USStageBRunError(
                "REV runs must retain both frozen cost-profile verdicts"
            )
        if self.cost_profile_verdicts is not None:
            base = self.cost_profile_verdicts.base_10bp_per_side
            if (
                base.strategy_id != self.strategy_id
                or base.contract_hash != self.contract_hash
                or base.labels != self.labels
            ):
                raise USStageBRunError("cost-profile verdict provenance mismatch")

    @property
    def strategy_id(self) -> str:
        return self.contract.candidate.strategy_id

    @property
    def contract_hash(self) -> str:
        return self.contract.candidate.contract_hash

    @property
    def labels(self) -> tuple[str, ...]:
        return self.contract.candidate.labels

    @property
    def completed_outcomes(self) -> tuple[TradeOutcome, ...]:
        return tuple(
            outcome for outcome in self.outcomes if outcome.status == "completed"
        )

    def to_dict(self) -> dict[str, Any]:
        status_counts = Counter(outcome.status for outcome in self.outcomes)
        return {
            "engine": "us-stage-b-v1",
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "labels": list(self.labels),
            "config_hash": self.contract.config_hash,
            "input_surface": "caller_supplied_read_only_source",
            "orders": 0,
            "account_mutations": 0,
            "database_writes": 0,
            "session_clock": "corpus_session_index_union_of_survivor_symbols",
            "corpus_sessions": [
                session.isoformat() for session in self.corpus_sessions
            ],
            "contract": self.contract.to_dict(),
            "observations": [item.to_dict() for item in self.observations],
            "outcomes": [item.to_dict() for item in self.outcomes],
            "cohort_comparisons": [item.to_dict() for item in self.cohorts],
            "outcome_status_counts": dict(sorted(status_counts.items())),
            "run_invalid": self.run_invalid,
            "invalid_reasons": list(self.invalid_reasons),
            "maturity_close_missing_policy": "RUN_INVALID",
            "access_summary": dict(self.access_summary),
            "verdict": self.verdict.to_dict(),
            "cost_profile_verdicts": (
                self.cost_profile_verdicts.to_dict()
                if self.cost_profile_verdicts is not None
                else None
            ),
        }


@dataclass(frozen=True)
class _Reservation:
    symbol: str
    entry_session: date
    exit_session: date | None

    def active_at_signal_close(self, session: date) -> bool:
        """A maturity at this close has finished before a new signal is ranked."""

        return self.entry_session <= session and (
            self.exit_session is None or session < self.exit_session
        )

    def active_at_entry_open(self, session: date) -> bool:
        """A maturity at this session's close still occupies an open-time slot."""

        return self.entry_session <= session and (
            self.exit_session is None or session <= self.exit_session
        )


def run_us_stage_b(
    *,
    source: USBarSource,
    contract: USStageBRunContract,
    corpus_sessions: Sequence[date],
) -> USStageBRunResult:
    """Run one frozen candidate with explicit index timing and no external I/O.

    A missing selected maturity close is a run-level invalidation.  In contrast,
    a selected entry with no valid next-session open is an ``entry_no_fill`` and
    is never replaced by a lower-ranked same-session signal.
    """

    if contract is None:  # type: ignore[comparison-overlap]
        raise USStageBRunError("an explicit US Stage-B run contract is required")
    sessions = _validate_corpus_sessions(corpus_sessions, contract)
    symbols = _validate_symbols(source.symbols())
    boundary_source = ExplorationBoundaryAccessSpy(
        source,
        exploration_start=contract.exploration_start,
        exploration_end=contract.exploration_end,
    )
    bars_by_symbol = _load_aligned_bars(boundary_source, symbols, sessions)

    observations: list[SignalObservation] = []
    outcomes: list[TradeOutcome] = []
    reservations: list[_Reservation] = []
    invalid_reasons: list[str] = []
    hold_sessions = _required_positive_int(contract, "hold_sessions")
    max_positions = _required_positive_int(contract, "max_positions")

    for session_index, session in enumerate(sessions):
        active_symbols = {
            reservation.symbol
            for reservation in reservations
            if reservation.active_at_signal_close(session)
        }
        daily_signals: list[SignalObservation] = []
        for symbol in symbols:
            observation = evaluate_signal(
                contract.candidate,
                symbol=symbol,
                session_date=session,
                history=bars_by_symbol[symbol][: session_index + 1],
                no_active_position=symbol not in active_symbols,
            )
            observations.append(observation)
            if observation.signal:
                daily_signals.append(observation)

        if not daily_signals:
            continue
        if session_index + 1 >= len(sessions):
            outcomes.extend(
                _outcome(
                    contract,
                    observation,
                    status="entry_no_fill",
                    selection_rank=None,
                    entry_session=None,
                )
                for observation in rank_signal_observations(daily_signals)
            )
            continue

        entry_session = sessions[session_index + 1]
        active_at_entry = sum(
            reservation.active_at_entry_open(entry_session)
            for reservation in reservations
        )
        if active_at_entry > max_positions:
            raise USStageBRunError(
                "existing reservations exceed the frozen max positions"
            )
        ranked = rank_signal_observations(daily_signals)
        selected = ranked[: max_positions - active_at_entry]
        rejected = ranked[max_positions - active_at_entry :]

        for rank, observation in enumerate(selected, start=1):
            outcome, reservation, invalid_reason = _execute_selected_signal(
                contract=contract,
                observation=observation,
                bars=bars_by_symbol[observation.symbol],
                entry_index=session_index + 1,
                exit_index=session_index + 1 + hold_sessions,
                sessions=sessions,
                selection_rank=rank,
            )
            outcomes.append(outcome)
            if reservation is not None:
                reservations.append(reservation)
            if invalid_reason is not None:
                invalid_reasons.append(invalid_reason)
        outcomes.extend(
            _outcome(
                contract,
                observation,
                status="capacity_rejected",
                selection_rank=None,
                entry_session=entry_session,
            )
            for observation in rejected
        )

    boundary_source.assert_no_outside_access()
    cohorts = _build_cohort_comparisons(
        contract=contract,
        outcomes=tuple(outcomes),
        observations=tuple(observations),
        bars_by_symbol=bars_by_symbol,
        sessions=sessions,
    )
    from .verdict import evaluate_falsification_evidence

    evaluation = evaluate_falsification_evidence(
        candidate=contract.candidate,
        outcomes=tuple(outcomes),
        cohorts=cohorts,
        run_invalid=bool(invalid_reasons),
        invalid_reasons=tuple(invalid_reasons),
    )
    return USStageBRunResult(
        contract=contract,
        corpus_sessions=sessions,
        observations=tuple(observations),
        outcomes=tuple(outcomes),
        cohorts=cohorts,
        run_invalid=bool(invalid_reasons),
        invalid_reasons=tuple(invalid_reasons),
        access_summary=boundary_source.summary(),
        verdict=evaluation.verdict,
        cost_profile_verdicts=evaluation.cost_profile_verdicts,
    )


def rank_signal_observations(
    observations: Sequence[SignalObservation],
) -> tuple[SignalObservation, ...]:
    """Rank only by ADV20-pre descending, then the packet SHA-byte tie break."""

    if any(not observation.signal for observation in observations):
        raise USStageBRunError("only true signal observations may enter ranking")
    digests = [observation.tie_break_sha256 for observation in observations]
    if len(set(digests)) != len(digests):
        raise USStageBRunError("SHA tie-break collision; no extra fallback is allowed")
    return tuple(
        sorted(
            observations,
            key=lambda observation: (
                -_required_adv(observation),
                bytes.fromhex(observation.tie_break_sha256),
            ),
        )
    )


def liquidity_decile_assignments(
    observations: Sequence[SignalObservation],
) -> Mapping[str, int]:
    """Assign same-session ADV deciles without a non-packet ranking fallback.

    Equal ADV values receive the same self-inclusive percentile and therefore
    the same decile.  Sparse samples stay sparse instead of being silently
    filled into a cohort; this makes an unavailable leave-one-out baseline
    visible to the falsification gate.
    """

    if not observations:
        return {}
    if any(not observation.universe_eligible for observation in observations):
        raise USStageBRunError("liquidity deciles require eligible observations only")
    values = [_required_adv(observation) for observation in observations]
    count = len(values)
    result: dict[str, int] = {}
    for observation, value in zip(observations, values, strict=True):
        percentile = sum(other <= value for other in values) / count
        decile = min(9, max(0, math.ceil(percentile * 10) - 1))
        result[observation.symbol] = decile
    return result


def _validate_corpus_sessions(
    corpus_sessions: Sequence[date], contract: USStageBRunContract
) -> tuple[date, ...]:
    sessions = tuple(corpus_sessions)
    if not sessions:
        raise USStageBRunError("corpus session index is empty")
    if any(
        isinstance(session, datetime) or not isinstance(session, date)
        for session in sessions
    ):
        raise USStageBRunError("corpus session index requires date values")
    if tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise USStageBRunError(
            "corpus session index must be strictly ascending and unique"
        )
    if (
        sessions[0] < contract.exploration_start
        or sessions[-1] > contract.exploration_end
    ):
        raise USStageBRunError("corpus session index falls outside the explicit window")
    return sessions


def _validate_symbols(raw_symbols: Sequence[str]) -> tuple[str, ...]:
    if any(not isinstance(symbol, str) or not symbol for symbol in raw_symbols):
        raise USStageBInputError("US Stage-B source returned an invalid symbol")
    if len(set(raw_symbols)) != len(raw_symbols):
        raise USStageBInputError("US Stage-B source returned duplicate symbols")
    return tuple(sorted(raw_symbols))


def _load_aligned_bars(
    source: ExplorationBoundaryAccessSpy,
    symbols: Sequence[str],
    sessions: Sequence[date],
) -> dict[str, tuple[USStageBDailyBar | None, ...]]:
    rows: dict[str, tuple[USStageBDailyBar | None, ...]] = {}
    for symbol in symbols:
        aligned: list[USStageBDailyBar | None] = []
        for session in sessions:
            bar = source.get(symbol, session)
            if bar is not None and (
                bar.symbol != symbol or bar.session_date != session
            ):
                raise USStageBInputError(
                    "US Stage-B source returned a bar outside its requested identity"
                )
            aligned.append(bar)
        rows[symbol] = tuple(aligned)
    return rows


def _execute_selected_signal(
    *,
    contract: USStageBRunContract,
    observation: SignalObservation,
    bars: Sequence[USStageBDailyBar | None],
    entry_index: int,
    exit_index: int,
    sessions: Sequence[date],
    selection_rank: int,
) -> tuple[TradeOutcome, _Reservation | None, str | None]:
    entry_session = sessions[entry_index]
    entry_bar = bars[entry_index]
    if entry_bar is None or not _finite_positive(entry_bar.open):
        return (
            _outcome(
                contract,
                observation,
                status="entry_no_fill",
                selection_rank=selection_rank,
                entry_session=entry_session,
            ),
            None,
            None,
        )
    entry_open = float(entry_bar.open)
    if exit_index >= len(sessions):
        reason = (
            "RUN_INVALID_MISSING_EXIT_TERMINAL:"
            f"{observation.symbol}:entry={entry_session.isoformat()}:"
            f"required_exit_session_index={exit_index}"
        )
        return (
            _outcome(
                contract,
                observation,
                status="run_invalid_missing_exit",
                selection_rank=selection_rank,
                entry_session=entry_session,
                entry_open=entry_open,
            ),
            _Reservation(
                symbol=observation.symbol,
                entry_session=entry_session,
                exit_session=None,
            ),
            reason,
        )
    exit_session = sessions[exit_index]
    exit_bar = bars[exit_index]
    reservation = _Reservation(
        symbol=observation.symbol,
        entry_session=entry_session,
        exit_session=exit_session,
    )
    if exit_bar is None or not _finite_positive(exit_bar.adjusted_close):
        reason = (
            f"RUN_INVALID_MISSING_EXIT:{observation.symbol}:{exit_session.isoformat()}"
        )
        return (
            _outcome(
                contract,
                observation,
                status="run_invalid_missing_exit",
                selection_rank=selection_rank,
                entry_session=entry_session,
                exit_session=exit_session,
                entry_open=entry_open,
            ),
            reservation,
            reason,
        )
    exit_adjusted_close = float(exit_bar.adjusted_close)
    gross_return = exit_adjusted_close / entry_open - 1.0
    return (
        _outcome(
            contract,
            observation,
            status="completed",
            selection_rank=selection_rank,
            entry_session=entry_session,
            exit_session=exit_session,
            entry_open=entry_open,
            exit_adjusted_close=exit_adjusted_close,
            gross_return=gross_return,
            base_net_return=(gross_return - contract.cost.base_round_trip_bp / 10_000),
            sensitivity_net_return=(
                gross_return - contract.cost.sensitivity_round_trip_bp / 10_000
            ),
        ),
        reservation,
        None,
    )


def _outcome(
    contract: USStageBRunContract,
    observation: SignalObservation,
    *,
    status: OutcomeStatus,
    selection_rank: int | None,
    entry_session: date | None,
    exit_session: date | None = None,
    entry_open: float | None = None,
    exit_adjusted_close: float | None = None,
    gross_return: float | None = None,
    base_net_return: float | None = None,
    sensitivity_net_return: float | None = None,
) -> TradeOutcome:
    adv20_pre_proxy = _required_adv(observation)
    volume_ratio = observation.metrics.get("volume_ratio20")
    return TradeOutcome(
        strategy_id=contract.candidate.strategy_id,
        contract_hash=contract.candidate.contract_hash,
        labels=contract.candidate.labels,
        symbol=observation.symbol,
        signal_session=observation.session_date,
        entry_session=entry_session,
        exit_session=exit_session,
        status=status,
        selection_rank=selection_rank,
        fixed_notional_usd=float(contract.candidate.parameter("fixed_notional_usd")),
        adv20_pre_proxy=adv20_pre_proxy,
        tie_break_sha256=observation.tie_break_sha256,
        entry_open=entry_open,
        exit_adjusted_close=exit_adjusted_close,
        gross_return=gross_return,
        base_net_return=base_net_return,
        sensitivity_net_return=sensitivity_net_return,
        volume_ratio20=volume_ratio,
    )


def _build_cohort_comparisons(
    *,
    contract: USStageBRunContract,
    outcomes: Sequence[TradeOutcome],
    observations: Sequence[SignalObservation],
    bars_by_symbol: Mapping[str, Sequence[USStageBDailyBar | None]],
    sessions: Sequence[date],
) -> tuple[CohortComparison, ...]:
    observations_by_session: dict[date, list[SignalObservation]] = defaultdict(list)
    observation_by_identity: dict[tuple[date, str], SignalObservation] = {}
    for observation in observations:
        observations_by_session[observation.session_date].append(observation)
        observation_by_identity[(observation.session_date, observation.symbol)] = (
            observation
        )
    session_index = {session: index for index, session in enumerate(sessions)}
    comparisons: list[CohortComparison] = []
    for outcome in outcomes:
        if outcome.status != "completed":
            continue
        if (
            outcome.entry_session is None
            or outcome.exit_session is None
            or outcome.base_net_return is None
            or outcome.sensitivity_net_return is None
        ):
            raise USStageBRunError("completed outcome lacks a resolved return identity")
        own_observation = observation_by_identity.get(
            (outcome.signal_session, outcome.symbol)
        )
        if own_observation is None:
            raise USStageBRunError("completed outcome has no source observation")
        eligible = tuple(
            observation
            for observation in observations_by_session[outcome.signal_session]
            if observation.universe_eligible
        )
        assignments = liquidity_decile_assignments(eligible)
        own_decile = assignments.get(outcome.symbol)
        if own_decile is None:
            raise USStageBRunError(
                "completed outcome is absent from its eligible cohort"
            )
        entry_index = session_index[outcome.entry_session]
        exit_index = session_index[outcome.exit_session]
        baseline_gross: list[float] = []
        excluded_no_fill = 0
        excluded_maturity = 0
        for member in eligible:
            if (
                member.symbol == outcome.symbol
                or assignments[member.symbol] != own_decile
            ):
                continue
            entry_bar = bars_by_symbol[member.symbol][entry_index]
            if entry_bar is None or not _finite_positive(entry_bar.open):
                excluded_no_fill += 1
                continue
            exit_bar = bars_by_symbol[member.symbol][exit_index]
            if exit_bar is None or not _finite_positive(exit_bar.adjusted_close):
                excluded_maturity += 1
                continue
            gross = float(exit_bar.adjusted_close) / float(entry_bar.open) - 1.0
            # §13 US cost amendment: a counterfactual cohort is gross.  The
            # frozen 10bp/5bp profiles apply only to selected strategy trades.
            baseline_gross.append(gross)
        if baseline_gross:
            baseline_mean = mean(baseline_gross)
            status: Literal["completed", "cohort_unavailable"] = "completed"
            base_excess = outcome.base_net_return - baseline_mean
            sensitivity_excess = outcome.sensitivity_net_return - baseline_mean
        else:
            baseline_mean = None
            status = "cohort_unavailable"
            base_excess = None
            sensitivity_excess = None
        comparisons.append(
            CohortComparison(
                strategy_id=contract.candidate.strategy_id,
                contract_hash=contract.candidate.contract_hash,
                labels=contract.candidate.labels,
                symbol=outcome.symbol,
                signal_session=outcome.signal_session,
                entry_session=outcome.entry_session,
                exit_session=outcome.exit_session,
                liquidity_decile=own_decile,
                eligible_universe_size=len(eligible),
                leave_one_out_member_count=len(baseline_gross),
                excluded_entry_no_fill_count=excluded_no_fill,
                excluded_maturity_close_count=excluded_maturity,
                status=status,
                candidate_base_net_return=outcome.base_net_return,
                candidate_sensitivity_net_return=outcome.sensitivity_net_return,
                baseline_base_net_return=baseline_mean,
                baseline_sensitivity_net_return=baseline_mean,
                base_excess_return=base_excess,
                sensitivity_excess_return=sensitivity_excess,
                volume_ratio20=outcome.volume_ratio20,
                tie_break_sha256=outcome.tie_break_sha256,
            )
        )
    return tuple(comparisons)


def _required_positive_int(contract: USStageBRunContract, name: str) -> int:
    value = contract.candidate.parameter(name)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise USStageBRunError(f"frozen parameter {name!r} is not a positive integer")
    return value


def _required_adv(observation: SignalObservation) -> float:
    if observation.adv20_pre_proxy is None or not math.isfinite(
        observation.adv20_pre_proxy
    ):
        raise USStageBRunError("rankable observation lacks a finite ADV20-pre proxy")
    return observation.adv20_pre_proxy


def _finite_positive(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) > 0.0
    )


def _date_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None
