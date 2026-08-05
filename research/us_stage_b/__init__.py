"""US-only, exploratory Stage-B engine for frozen survivor-biased candidates."""

from .contracts import USCostLiteral, USStageBRunContract
from .engine import (
    CohortComparison,
    TradeOutcome,
    USStageBRunResult,
    liquidity_decile_assignments,
    rank_signal_observations,
    run_us_stage_b,
)
from .registry import CandidateRegistry
from .source import InMemoryUSBarSource, USStageBDailyBar
from .verdict import FalsificationVerdict, RevCostProfileVerdicts

__all__ = [
    "CandidateRegistry",
    "CohortComparison",
    "FalsificationVerdict",
    "InMemoryUSBarSource",
    "TradeOutcome",
    "RevCostProfileVerdicts",
    "USCostLiteral",
    "USStageBDailyBar",
    "USStageBRunContract",
    "USStageBRunResult",
    "liquidity_decile_assignments",
    "rank_signal_observations",
    "run_us_stage_b",
]
