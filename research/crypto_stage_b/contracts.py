"""Explicit, frozen run contracts for crypto Stage-B research.

There are deliberately no implicit cost defaults.  A caller has to place the
operator-frozen literals in every run contract, where they become part of the
serialised configuration hash and the resulting evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Final

from research.crypto_corpus.constants import EXPLORATION_END
from research_contracts.canonical_hash import canonical_sha256

from .registry import ADMITTED_STRATEGY_IDS, EXPECTED_RETURN_SHA256, CandidateDefinition

__all__ = [
    "CRYPTO_CORPUS_HOLDOUT_START",
    "FROZEN_VENUE_COST_LITERALS",
    "CryptoStageBRunContract",
    "ExplorationWindowError",
    "VenueCostLiteral",
]


CRYPTO_CORPUS_HOLDOUT_START: Final[date] = EXPLORATION_END.date()
"""The first crypto-corpus holdout UTC calendar day; it is never readable."""

FROZEN_VENUE_COST_LITERALS: Final[dict[str, dict[str, int]]] = {
    "upbit_krw": {
        "fee_bp_per_side": 5,
        "slippage_bp_per_side": 10,
        "sensitivity_slippage_bp_per_side": 30,
    },
    "binance_usdt_spot": {
        "fee_bp_per_side": 10,
        "slippage_bp_per_side": 10,
        "sensitivity_slippage_bp_per_side": 30,
    },
}
"""Validation-only copies of the operator-frozen, pre-result cost literals."""


class ExplorationWindowError(ValueError):
    """A caller attempted an invalid or holdout-intersecting research window."""


@dataclass(frozen=True)
class VenueCostLiteral:
    """One explicitly supplied venue-specific base and sensitivity cost model."""

    venue: str
    fee_bp_per_side: int
    slippage_bp_per_side: int
    sensitivity_slippage_bp_per_side: int

    def __post_init__(self) -> None:
        expected = FROZEN_VENUE_COST_LITERALS.get(self.venue)
        actual = {
            "fee_bp_per_side": self.fee_bp_per_side,
            "slippage_bp_per_side": self.slippage_bp_per_side,
            "sensitivity_slippage_bp_per_side": self.sensitivity_slippage_bp_per_side,
        }
        if expected is None or actual != expected:
            raise ValueError(
                "cost literal must exactly match the operator-frozen venue profile; "
                "the engine does not inject defaults"
            )

    @property
    def round_trip_bp(self) -> int:
        return 2 * (self.fee_bp_per_side + self.slippage_bp_per_side)

    @property
    def sensitivity_round_trip_bp(self) -> int:
        return 2 * (self.fee_bp_per_side + self.sensitivity_slippage_bp_per_side)

    def to_dict(self) -> dict[str, int | str]:
        return {
            "venue": self.venue,
            "fee_bp_per_side": self.fee_bp_per_side,
            "slippage_bp_per_side": self.slippage_bp_per_side,
            "round_trip_bp": self.round_trip_bp,
            "sensitivity_slippage_bp_per_side": self.sensitivity_slippage_bp_per_side,
            "sensitivity_round_trip_bp": self.sensitivity_round_trip_bp,
            "low_liquidity_break_even_round_trip_bp": self.round_trip_bp,
        }


@dataclass(frozen=True)
class CryptoStageBRunContract:
    """One candidate × venue × exploration-window execution contract.

    The contract intentionally models one venue at a time.  This prevents a
    KRW value from becoming comparable to a USDT value anywhere in selection or
    accounting, and makes a venue-specific result the only possible output.
    """

    candidate: CandidateDefinition
    venue: str
    exploration_start: date
    exploration_end: date
    cost: VenueCostLiteral

    def __post_init__(self) -> None:
        if isinstance(self.exploration_start, datetime) or isinstance(
            self.exploration_end, datetime
        ):
            raise ExplorationWindowError(
                "exploration bounds must be UTC dates, not datetimes"
            )
        if self.exploration_start > self.exploration_end:
            raise ExplorationWindowError("exploration_start is after exploration_end")
        if self.exploration_end >= CRYPTO_CORPUS_HOLDOUT_START:
            raise ExplorationWindowError(
                "exploration window intersects crypto-corpus holdout; "
                "holdout reads are forbidden"
            )
        if self.candidate.strategy_id not in ADMITTED_STRATEGY_IDS:
            raise ValueError(
                "only the three admitted candidates are executable; preserved "
                "HTA-01 is not an implementation target"
            )
        if self.candidate.source_return_sha256 != EXPECTED_RETURN_SHA256:
            raise ValueError("candidate is not bound to the frozen upstream return")
        if self.candidate.venue_scope != "both":
            raise ValueError(
                "candidate venue scope is not the admitted both-venue scope"
            )
        if self.cost.venue != self.venue:
            raise ValueError("run-contract cost venue does not match run venue")
        if self.candidate.required_history_days <= 0:
            raise ValueError("candidate required_history_days must be positive")

    @property
    def config_hash(self) -> str:
        return canonical_sha256(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "engine": "crypto-stage-b-v1",
            "strategy_id": self.candidate.strategy_id,
            "contract_hash": self.candidate.contract_hash,
            "source_return_sha256": self.candidate.source_return_sha256,
            "venue": self.venue,
            "exploration_window": {
                "start": self.exploration_start.isoformat(),
                "end": self.exploration_end.isoformat(),
            },
            "entry_timing": "next_utc_calendar_day_open",
            "exit_timing": f"entry_day_D_plus_{int(self.candidate.parameter('exit_D_plus_days'))}_utc_close",
            "max_concurrent_positions_per_venue": int(
                self.candidate.parameter("max_concurrent_positions_per_venue")
            ),
            "symbol_position_limit": 1,
            "fixed_normalized_notional_unit": 1.0,
            "cost_literal": self.cost.to_dict(),
        }
