"""Explicit run and cost contracts for the isolated US Stage-B engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Final

from research_contracts.canonical_hash import canonical_sha256

from .registry import (
    FROZEN_CANDIDATES_SHA256,
    US_CANDIDATE_ORDER,
    CandidateBinding,
)

__all__ = [
    "US_EXPLORATION_END",
    "US_EXPLORATION_START",
    "US_HOLDOUT_START",
    "ExplorationWindowError",
    "USCostLiteral",
    "USStageBRunContract",
]


US_EXPLORATION_START: Final = date(2016, 1, 1)
US_EXPLORATION_END: Final = date(2024, 12, 31)
US_HOLDOUT_START: Final = date(2025, 1, 1)
_BASE_BP_PER_SIDE: Final = 10
_SENSITIVITY_BP_PER_SIDE: Final = 5


class ExplorationWindowError(ValueError):
    """A Stage-B caller attempted an invalid or holdout-intersecting window."""


@dataclass(frozen=True)
class USCostLiteral:
    """The two operator-frozen US slippage literals, supplied by every caller.

    This class intentionally has no defaults.  A construction site must state
    both values, and values other than the packet's 10bp/side base and
    5bp/side sensitivity are rejected before an engine can run.
    """

    base_bp_per_side: int
    sensitivity_bp_per_side: int

    def __post_init__(self) -> None:
        if (
            self.base_bp_per_side != _BASE_BP_PER_SIDE
            or self.sensitivity_bp_per_side != _SENSITIVITY_BP_PER_SIDE
        ):
            raise ValueError(
                "US cost literal must be exactly 10bp/side base and 5bp/side "
                "sensitivity; no engine default is available"
            )

    @property
    def base_round_trip_bp(self) -> int:
        return 2 * self.base_bp_per_side

    @property
    def sensitivity_round_trip_bp(self) -> int:
        return 2 * self.sensitivity_bp_per_side

    def to_dict(self) -> dict[str, int]:
        return {
            "base_bp_per_side": self.base_bp_per_side,
            "base_round_trip_bp": self.base_round_trip_bp,
            "sensitivity_bp_per_side": self.sensitivity_bp_per_side,
            "sensitivity_round_trip_bp": self.sensitivity_round_trip_bp,
        }


@dataclass(frozen=True)
class USStageBRunContract:
    """One explicit candidate × exploration-window run contract.

    The supplied session index, not a calendar or exchange-calendar import,
    determines D+N timing in the engine.  The date bounds are access barriers:
    any window that touches 2025+ is refused before source data is read.
    """

    candidate: CandidateBinding
    exploration_start: date
    exploration_end: date
    cost: USCostLiteral

    def __post_init__(self) -> None:
        if isinstance(self.exploration_start, datetime) or isinstance(
            self.exploration_end, datetime
        ):
            raise ExplorationWindowError(
                "US exploration bounds must be dates, not datetimes"
            )
        if self.exploration_start > self.exploration_end:
            raise ExplorationWindowError("exploration_start is after exploration_end")
        if self.exploration_start < US_EXPLORATION_START:
            raise ExplorationWindowError(
                "US exploration starts no earlier than 2016-01-01"
            )
        if self.exploration_end > US_EXPLORATION_END:
            raise ExplorationWindowError(
                "US exploration window intersects the 2025+ sealed holdout"
            )
        if self.candidate.strategy_id not in US_CANDIDATE_ORDER:
            raise ValueError("only the three frozen US candidates are executable")
        if self.candidate.source_packet_sha256 != FROZEN_CANDIDATES_SHA256:
            raise ValueError("candidate is not bound to the frozen US YAML packet")
        if (
            self.candidate.contract_hash
            != hashlib.sha256(self.candidate.source_block).hexdigest()
        ):
            raise ValueError("candidate raw-block contract hash mismatch")
        if self.candidate.parameter("fixed_notional_usd") != 500:
            raise ValueError("US fixed-notional contract drift")
        if self.candidate.parameter("max_positions") != 10:
            raise ValueError("US max-position contract drift")

    @property
    def config_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return self.candidate.stamp(
            {
                "engine": "us-stage-b-v1",
                "exploration_window": {
                    "start": self.exploration_start.isoformat(),
                    "end": self.exploration_end.isoformat(),
                },
                "session_clock": "corpus_session_index_union_of_survivor_symbols",
                "entry_timing": "t_plus_1_open",
                "exit_timing": (
                    "entry_session_D_plus_"
                    f"{self.candidate.parameter('hold_sessions')}_session_close"
                ),
                "fixed_notional_usd": self.candidate.parameter("fixed_notional_usd"),
                "max_positions": self.candidate.parameter("max_positions"),
                "cost_literal": self.cost.to_dict(),
                "maturity_close_missing": "RUN_INVALID",
                "survivorship_assumption": "SURVIVORSHIP_BIASED=TRUE",
            }
        )
