"""ROB-942 (H2, ROB-940) — corrected cost model (pure, stdlib).

Frozen ROB-940 cost contract (orch 2026-07-17 fee-recalibration comment,
superseding the GPT Pro research draft's 4bp/8bp/11bp/15bp/60bp figures):

  * taker fee: 5bp per leg -> 10bp round trip (``FEE_ROUND_TRIP_BPS``).
  * round-trip all-in scenarios: base=13bp, primary stress=17bp,
    upward stress=22bp. ``COST_SCENARIO_PRIMARY_STRESS`` (17bp) is the
    selection/scoring cost; 13/22bp are pre-registered sensitivity scenarios
    over the SAME trade path (see ``rob940_engine.run_symbol_stream``, which
    takes one ``CostScenario`` per invocation and reproduces the identical
    entries/exits across scenarios).
  * ``MIN_TP_DISTANCE_BPS`` (68bp) is DERIVED as 4x the primary stress
    all-in, not a bare literal, so the two numbers cannot silently drift apart
    (AC7 pins the derived value, not the multiplier).

Funding is a PIT-safe sidecar concern (H1 supplies realized crossings via an
explicit pure input); this module only defines the crossing shape and the
sign convention for turning crossings into a signed cost, plus the single
"subtract everything exactly once" net formula (AC6: no fee+all_in+funding
double count).

No DB/network/app/broker imports — pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Side = Literal["long", "short"]

FEE_ENTRY_BPS = 5.0
FEE_EXIT_BPS = 5.0
FEE_ROUND_TRIP_BPS = FEE_ENTRY_BPS + FEE_EXIT_BPS  # 10.0


@dataclass(frozen=True)
class CostScenario:
    name: str
    all_in_bps: float  # round-trip all-in bps; already embeds FEE_ROUND_TRIP_BPS


COST_SCENARIO_BASE = CostScenario("base", 13.0)
COST_SCENARIO_PRIMARY_STRESS = CostScenario("primary_stress", 17.0)
COST_SCENARIO_UPWARD_STRESS = CostScenario("upward_stress", 22.0)
COST_SCENARIOS: tuple[CostScenario, ...] = (
    COST_SCENARIO_BASE,
    COST_SCENARIO_PRIMARY_STRESS,
    COST_SCENARIO_UPWARD_STRESS,
)

# AC7: computed TP distance <68bp is no-trade; ==68bp is allowed. Derived, not
# hardcoded, from 4x the primary stress scenario (frozen relationship).
MIN_TP_DISTANCE_BPS = 4.0 * COST_SCENARIO_PRIMARY_STRESS.all_in_bps  # 68.0


@dataclass(frozen=True)
class FundingCrossing:
    ts: int  # crossing timestamp, epoch ms UTC, entry_ts < ts <= exit_ts
    rate_bps: float  # signed realized funding rate; positive => longs pay shorts


def gross_bps(side: Side, entry_price: float, exit_price: float) -> float:
    """Signed gross price-return in bps, entry-based (linear) and long/short symmetric.

    Entry-based (not exit-based) so a fill that lands EXACTLY on a barrier
    computed as ``entry * (1 +/- d_bps/1e4)`` (see ``rob940_engine``) realizes
    gross_bps with magnitude exactly ``d_bps`` on both sides — required for
    the R-multiple accounting (``R = net_bps / sl_distance_bps``, AC8) to mean
    the same thing for longs and shorts.
    """
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")
    if side == "long":
        return (exit_price - entry_price) / entry_price * 1e4
    if side == "short":
        return (entry_price - exit_price) / entry_price * 1e4
    raise ValueError(f"unknown side {side!r}")


def realized_funding_bps(side: Side, crossings: Sequence[FundingCrossing]) -> float:
    """Signed realized funding cost for the hold (positive = cost, negative = credit).

    Longs pay when the realized rate is positive (shorts receive); the
    convention flips sign for shorts.
    """
    total_rate = sum(c.rate_bps for c in crossings)
    if side == "long":
        return total_rate
    if side == "short":
        return -total_rate
    raise ValueError(f"unknown side {side!r}")


def net_bps(gross: float, cost_scenario: CostScenario, funding: float) -> float:
    """AC6: net = gross - all_in(scenario) - funding, each subtracted exactly once.

    ``cost_scenario.all_in_bps`` already embeds the round-trip fee — do not
    additionally subtract ``FEE_ROUND_TRIP_BPS`` here (that would double-count).
    """
    return gross - cost_scenario.all_in_bps - funding
