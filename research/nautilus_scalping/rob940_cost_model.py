"""ROB-942 (H2, ROB-940) — corrected cost model (pure, stdlib).

Frozen ROB-940 cost contract (orch 2026-07-17 fee-recalibration comment,
superseding the GPT Pro research draft's 4bp/8bp/11bp/15bp/60bp figures):

  * taker fee: 5bp per leg -> 10bp round trip (``FEE_ROUND_TRIP_BPS``).
  * round-trip all-in scenarios: base=13bp, primary stress=17bp,
    upward stress=22bp. ``COST_SCENARIO_PRIMARY_STRESS`` (17bp) is the
    selection/scoring cost; 13/22bp are pre-registered sensitivity scenarios.
  * ROB-942 R1 correction (2026-07-17, supersedes an earlier "identical
    entries/exits across scenarios" claim in this docstring that a verifier
    proved false by repro): each ``CostScenario`` is simulated via its OWN
    independent ``rob940_engine.run_symbol_stream`` invocation/ledger, not a
    net-only revaluation of one shared path. The signal ELIGIBILITY gate
    (``MIN_TP_DISTANCE_BPS``, 68bp) is a fixed value derived once from the
    primary-stress scenario — it does NOT vary per scenario, so which signals
    are even considered for entry is identical across the three runs. But
    AC8's cost-included ``<=-2.0R`` daily stop uses each scenario's own
    ``net_bps`` (which subtracts that scenario's ``all_in_bps``), so the point
    at which a day HALTS can differ by scenario: a higher-cost scenario can
    reach ``-2.0R`` sooner than a lower-cost one on the exact same bars/
    signals, producing fewer trades (a shorter path) for that scenario alone.
    This is intentional (AC8 is deliberately cost-included, not cost-blind)
    and each run's ledger is self-consistent; it is simply NOT a same-path
    sensitivity revaluation. See ``rob940_engine.run_symbol_stream`` for the
    mechanism and the pinning regression tests in
    ``tests/test_rob940_engine.py`` (68bp-gate-identical-across-scenarios /
    cost-scenario-dependent-daily-stop-diverges-trade-count).
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

import math
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

    def __post_init__(self) -> None:
        # ROB-942 R1 M1: reject NaN/+-Inf fail-closed at construction.
        if not math.isfinite(self.all_in_bps):
            raise ValueError(
                f"CostScenario.all_in_bps must be finite, got {self.all_in_bps!r}"
            )


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
    # ROB-944 Q2 (orch-fable-answer-rob944-20260717.md, 2026-07-17): corrected
    # stale boundary doc (was "entry_ts < ts <= exit_ts"). H1's
    # FundingSidecar.realized_crossings is the frozen, tested authority: the
    # half-open position window [entry_ts, exit_ts) is what "held" means -- a
    # crossing exactly at entry_ts is included (funding is charged if the
    # position exists at the snapshot instant) and one exactly at exit_ts is
    # excluded. This module still does not enforce the boundary itself (it
    # accepts whatever crossings its caller supplies); H4 is the one caller
    # that builds crossings, and it does so directly from H1's sidecar
    # without reinterpreting the endpoint.
    ts: int  # crossing timestamp, epoch ms UTC, entry_ts <= ts < exit_ts
    rate_bps: float  # signed realized funding rate; positive => longs pay shorts

    def __post_init__(self) -> None:
        # ROB-942 R1 M1: reject NaN/+-Inf fail-closed at construction.
        if not math.isfinite(self.rate_bps):
            raise ValueError(
                f"FundingCrossing.rate_bps must be finite, got {self.rate_bps!r}"
            )


def gross_bps(side: Side, entry_price: float, exit_price: float) -> float:
    """Signed gross price-return in bps, entry-based (linear) and long/short symmetric.

    Entry-based (not exit-based) so a fill that lands EXACTLY on a barrier
    computed as ``entry * (1 +/- d_bps/1e4)`` (see ``rob940_engine``) realizes
    gross_bps with magnitude exactly ``d_bps`` on both sides — required for
    the R-multiple accounting (``R = net_bps / sl_distance_bps``, AC8) to mean
    the same thing for longs and shorts.
    """
    if not math.isfinite(entry_price):
        raise ValueError(f"entry_price must be finite, got {entry_price!r}")
    if not math.isfinite(exit_price):
        raise ValueError(f"exit_price must be finite, got {exit_price!r}")
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
    if not math.isfinite(gross):
        raise ValueError(f"gross must be finite, got {gross!r}")
    if not math.isfinite(funding):
        raise ValueError(f"funding must be finite, got {funding!r}")
    return gross - cost_scenario.all_in_bps - funding
