"""Venue × UTC-calendar-year HARNESS_QUERY evidence for Stage-B pair results."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from statistics import fmean
from typing import Any

from .engine import CandidatePairResult, ExecutionArmResult

__all__ = ["HarnessReport", "VenueYearRecord", "build_harness_report"]


@dataclass(frozen=True)
class VenueYearRecord:
    """One non-aggregated venue × signal-UTC-year evidence row."""

    strategy_id: str
    contract_hash: str
    venue: str
    calendar_year: int
    full: dict[str, Any]
    ablation: dict[str, Any]
    incremental: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "venue": self.venue,
            "calendar_year": self.calendar_year,
            "full": self.full,
            "ablation": self.ablation,
            "incremental": self.incremental,
        }


@dataclass(frozen=True)
class HarnessReport:
    """The required decomposition; it deliberately has no all-years aggregate."""

    strategy_id: str
    contract_hash: str
    venue: str
    source_return_sha256: str
    records: tuple[VenueYearRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "engine": "crypto-stage-b-v1",
            "strategy_id": self.strategy_id,
            "contract_hash": self.contract_hash,
            "source_return_sha256": self.source_return_sha256,
            "venue": self.venue,
            "calendar_year_basis": "signal_session_utc_year",
            "query_schema": "strategy_id_x_venue_x_calendar_year",
            "records": [record.to_dict() for record in self.records],
        }


def build_harness_report(pair: CandidatePairResult) -> HarnessReport:
    """Build every requested count per venue and calendar year, never all-years."""
    contract = pair.contract
    if pair.full.contract != contract or pair.ablation.contract != contract:
        raise ValueError("full and ablation results must share one exact run contract")
    years = sorted(
        {
            observation.signal_session.year
            for arm in (pair.full, pair.ablation)
            for observation in arm.observations
        }
    )
    records = tuple(
        VenueYearRecord(
            strategy_id=contract.candidate.strategy_id,
            contract_hash=contract.candidate.contract_hash,
            venue=contract.venue,
            calendar_year=year,
            full=_arm_year_metrics(pair.full, year),
            ablation=_arm_year_metrics(pair.ablation, year),
            incremental=_incremental_year_metrics(pair.full, pair.ablation, year),
        )
        for year in years
    )
    return HarnessReport(
        strategy_id=contract.candidate.strategy_id,
        contract_hash=contract.candidate.contract_hash,
        venue=contract.venue,
        source_return_sha256=contract.candidate.source_return_sha256,
        records=records,
    )


def _arm_year_metrics(arm: ExecutionArmResult, year: int) -> dict[str, Any]:
    observations = tuple(
        item for item in arm.observations if item.signal_session.year == year
    )
    outcomes = tuple(item for item in arm.outcomes if item.signal_session.year == year)
    outcomes_by_status = Counter(item.status for item in outcomes)
    filled = tuple(item for item in outcomes if item.entry_open is not None)
    resolved = tuple(item for item in outcomes if item.net_return is not None)
    symbols = Counter(item.symbol for item in filled)
    symbol_trade_counts = dict(sorted(symbols.items()))
    max_symbol_share = max(symbols.values()) / len(filled) if filled else None
    generic = {
        "strategy_id": arm.contract.candidate.strategy_id,
        "contract_hash": arm.contract.candidate.contract_hash,
        "venue": arm.contract.venue,
        "calendar_year": year,
        "eligible_symbol_days": sum(item.eligible for item in observations),
        "signal_count": sum(item.signal for item in observations),
        "next_day_open_fill_count": len(filled),
        "completed_exit_count": outcomes_by_status["completed"],
        "missing_history_exclusion_count": sum(
            _is_missing_history_exclusion(item.exclusion_reason)
            for item in observations
        ),
        "entry_no_fill_count": outcomes_by_status["entry_no_fill"],
        "missing_exit_count": outcomes_by_status["missing_exit"],
        "delisted_exit_count": outcomes_by_status["delisted_exit"],
        "capacity_rejected_count": outcomes_by_status["capacity_rejected"],
        "symbol_position_rejected_count": outcomes_by_status[
            "symbol_position_rejected"
        ],
        "censored_before_entry_boundary_count": outcomes_by_status[
            "censored_before_entry_boundary"
        ],
        "censored_at_exploration_boundary_count": outcomes_by_status[
            "censored_at_exploration_boundary"
        ],
        "distinct_traded_symbols": len(symbols),
        "symbol_trade_counts": symbol_trade_counts,
        "max_symbol_trade_share": max_symbol_share,
        "gross_mean_return": _mean_or_none(item.gross_return for item in resolved),
        "net_mean_return": _mean_or_none(item.net_return for item in resolved),
        "sensitivity_net_mean_return": _mean_or_none(
            item.sensitivity_net_return for item in resolved
        ),
        "resolved_exit_count": len(resolved),
        "fixed_normalized_notional_unit": 1.0,
        "cost_literal": arm.contract.cost.to_dict(),
        "low_liquidity_break_even_round_trip_bp": arm.contract.cost.round_trip_bp,
        "sensitivity_break_even_round_trip_bp": arm.contract.cost.sensitivity_round_trip_bp,
    }
    strategy_id = arm.contract.candidate.strategy_id
    if strategy_id == "CR-SPOT-ETR-01":
        return generic
    if strategy_id == "CR-SPOT-TPR-01":
        return {
            **generic,
            "trend_state_days": _stage_count(observations, "trend_state"),
            "pullback_setup_days": _stage_count(observations, "pullback_setup"),
            "final_signal_count": generic["signal_count"],
        }
    if strategy_id == "CR-SPOT-CEB-01":
        return {
            **generic,
            "complete_history_days": sum(item.eligible for item in observations),
            "compression_state_days": _stage_count(observations, "compression_state"),
            "raw_20d_breakout_days": _stage_count(observations, "raw_20d_breakout"),
            "full_signal_count": generic["signal_count"],
            "fill_count": len(filled),
            "distinct_symbols": len(symbols),
        }
    raise ValueError(f"unsupported harness strategy: {strategy_id!r}")


def _incremental_year_metrics(
    full: ExecutionArmResult, ablation: ExecutionArmResult, year: int
) -> dict[str, Any]:
    full_returns = _resolved_returns_for_year(full, year)
    ablation_returns = _resolved_returns_for_year(ablation, year)
    full_sensitivity_returns = _resolved_sensitivity_returns_for_year(full, year)
    ablation_sensitivity_returns = _resolved_sensitivity_returns_for_year(
        ablation, year
    )
    full_mean = _mean_or_none(full_returns)
    ablation_mean = _mean_or_none(ablation_returns)
    full_sensitivity_mean = _mean_or_none(full_sensitivity_returns)
    ablation_sensitivity_mean = _mean_or_none(ablation_sensitivity_returns)
    delta = (
        None
        if full_mean is None or ablation_mean is None
        else full_mean - ablation_mean
    )
    return {
        "strategy_id": full.contract.candidate.strategy_id,
        "contract_hash": full.contract.candidate.contract_hash,
        "venue": full.contract.venue,
        "calendar_year": year,
        "full_net_mean_return": full_mean,
        "ablation_net_mean_return": ablation_mean,
        "full_minus_ablation_net_mean_return": delta,
        "full_sensitivity_net_mean_return": full_sensitivity_mean,
        "ablation_sensitivity_net_mean_return": ablation_sensitivity_mean,
        "full_minus_ablation_sensitivity_net_mean_return": (
            None
            if full_sensitivity_mean is None or ablation_sensitivity_mean is None
            else full_sensitivity_mean - ablation_sensitivity_mean
        ),
        "incremental_state": (
            "INCONCLUSIVE_EMPTY_ARM"
            if delta is None
            else "FULL_EXCEEDS_ABLATION"
            if delta > 0.0
            else "FULL_DOES_NOT_EXCEED_ABLATION"
        ),
    }


def _resolved_returns_for_year(arm: ExecutionArmResult, year: int) -> tuple[float, ...]:
    return tuple(
        item.net_return
        for item in arm.outcomes
        if item.signal_session.year == year and item.net_return is not None
    )


def _resolved_sensitivity_returns_for_year(
    arm: ExecutionArmResult, year: int
) -> tuple[float, ...]:
    return tuple(
        item.sensitivity_net_return
        for item in arm.outcomes
        if item.signal_session.year == year and item.sensitivity_net_return is not None
    )


def _mean_or_none(values: Any) -> float | None:
    materialized = tuple(value for value in values if value is not None)
    return fmean(materialized) if materialized else None


def _stage_count(observations: tuple[Any, ...], name: str) -> int:
    return sum(item.stages.get(name, False) for item in observations)


def _is_missing_history_exclusion(reason: str | None) -> bool:
    return reason in {
        "missing_required_history",
        "non_contiguous_required_history",
        "invalid_required_close_history",
        "invalid_required_price_history",
    }
