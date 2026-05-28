"""ROB-351 (eng-review Issue 3 + Codex realistic-path) — the ROB-343 hand-off label.

Decides, for a Stage-2 survivor, whether it is worth running through ROB-343
(the deferred execution-realism harness). The 343-worthy test is NOT tautological
"cost is the blocker"; it requires a PLAUSIBLE PATH TO TRADEABILITY:

  * ``promote_to_pilot``            — already net-viable at realistic taker fees;
                                      ROB-343 not required.
  * ``cost_binding_343_candidate``  — positive gross, killed by taker fees, BUT the
                                      maker-conservative scenario (maker_fill queue
                                      loss + adverse selection) is itself net-positive
                                      => realistic maker execution closes the gap.
  * ``needs_more_data``             — OOS evidence insufficient.
  * ``reject``                      — no gross edge, or gap not closable by realistic
                                      maker execution.

Closability lives in the PURE maker path (maker_fill -> validated_gate), never in
fee_sweep's taker-only linear rescale (Issue 3). ``breakeven_taker_fee_bps`` is
reported as evidence; the maker-conservative net is the load-bearing test.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

import cost_model
from validated_gate import Trade

Label = Literal[
    "promote_to_pilot", "cost_binding_343_candidate", "needs_more_data", "reject"
]


def breakeven_taker_fee_bps_from_sums(
    sum_net_ref: float, sum_comm: float, ref_fee_bps: float = cost_model.REF_FEE_BPS
) -> float:
    """Closed-form max per-leg TAKER fee at which total net == 0 (evidence only).

    net(fee) = sum_net_ref + sum_comm * (1 - fee/ref) = 0
        => fee = ref * (1 + sum_net_ref / sum_comm)
    """
    if sum_comm == 0.0:
        # fee cannot move net; infinite headroom if already positive, else none
        return math.inf if sum_net_ref > 0 else 0.0
    return max(0.0, ref_fee_bps * (1.0 + sum_net_ref / sum_comm))


def breakeven_taker_fee_bps(trades: list[Trade], ref_fee_bps: float = cost_model.REF_FEE_BPS) -> float:
    """Breakeven taker fee for a ``Trade`` list (taker-only; maker decided elsewhere)."""
    return breakeven_taker_fee_bps_from_sums(
        sum(t.net_ref_pnl for t in trades), sum(t.commission_ref for t in trades), ref_fee_bps
    )


@dataclass(frozen=True)
class Rob343Verdict:
    label: Label
    cost_binding: bool
    closable: bool
    breakeven_taker_bps: float
    maker_conservative_net: float
    reason: str


def label_343_candidate(
    *,
    taker_net_pnl: float,
    gross_pnl: float,
    maker_conservative_net: float,
    oos_significant: bool,
    breakeven_taker_bps: float,
) -> Rob343Verdict:
    """Classify a survivor. See module docstring for the decision table."""
    if not oos_significant:
        return Rob343Verdict(
            "needs_more_data", False, False, breakeven_taker_bps, maker_conservative_net,
            "OOS evidence insufficient (sample/CI/FDR gate not cleared)",
        )
    if taker_net_pnl > 0:
        return Rob343Verdict(
            "promote_to_pilot", False, maker_conservative_net > 0,
            breakeven_taker_bps, maker_conservative_net,
            "already net-viable at realistic taker fees; ROB-343 not required",
        )
    cost_binding = gross_pnl > 0 and taker_net_pnl <= 0
    if not cost_binding:
        return Rob343Verdict(
            "reject", False, False, breakeven_taker_bps, maker_conservative_net,
            "no positive gross edge; not a cost problem",
        )
    closable = maker_conservative_net > 0
    if closable:
        return Rob343Verdict(
            "cost_binding_343_candidate", True, True,
            breakeven_taker_bps, maker_conservative_net,
            "positive gross killed by taker fees; maker-conservative scenario is "
            "net-positive => realistic maker execution plausibly closes the gap",
        )
    return Rob343Verdict(
        "reject", True, False, breakeven_taker_bps, maker_conservative_net,
        "cost-binding but maker-conservative scenario still net-negative => no "
        "realistic execution path (Codex realistic-path stop-rule)",
    )
