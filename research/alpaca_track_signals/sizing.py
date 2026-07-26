"""ROB-1061 H3 (SS11.5, SS12.5) — entry-time vol sizing, the $25
``MIN_TARGET_NOTIONAL`` floor (read from the H2 seal, never hardcoded), and
the D/Score-descending, symbol-ascending cash-constrained allocation
tie-break (AC12).

The fixed $2,000 initial equity and $62.50 AP-A1 base slot (SS11.5: "초기
equity $2,000 고정. base_slot = equity/32 = $62.50") ARE inside H2's seal —
``identity.build_components_for_config``'s ``frozen_config`` component
carries both literals, and that component is folded into
``SEALED_ARTIFACT_SEMANTIC_HASH`` by ``artifact.SealedArtifact.to_dict()``.
An earlier version of this module claimed otherwise and hardcoded its own
copy (``INITIAL_EQUITY_USD = 2000.0``, ``AP_A1_BASE_SLOT_DIVISOR = 32``) —
a real AC18 "H3~H6는 이 봉인 레코드를 읽기 전용으로만 소비한다" violation:
a simulated re-seal (the frozen_config literals changed to 1600/50.00) moved
H2's own tests as designed but left this module's numbers frozen at
2000.0/62.50, contributing zero detection. ``seal_consumption.
initial_equity_usd``/``ap_a1_base_slot_usd`` now read both values from the
seal every time (see ``test_sizing.py``'s divergence test, which simulates
exactly that re-seal and asserts this module's numbers move with it).

The $25 floor is likewise one of H2's sealed values
(``run_status.min_strategy_target_usd``) and is read via
``seal_consumption.min_strategy_target_usd`` here, never re-typed.

PnL-blind by construction: "available cash" is always the sealed initial
equity minus the sum of entry-time COMMITTED notional for currently-open
positions — never a running mark-to-market balance, never adjusted for price
moves or realized/unrealized P&L (positions are never resized/rebalanced
while held, SS11.5/SS12.5 "보유 중 리밸런스 없음"). No ``pnl``/``return``/
``forward-*``/``exit-price`` field anywhere in this module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import seal_consumption as sc

__all__ = [
    "AllocationOutcome",
    "allocate_cash_constrained",
    "ap_a1_base_slot_usd",
    "ap_a2_base_slot_usd",
    "available_cash",
    "compute_vol_scale",
    "meets_min_target_notional",
    "target_notional_ap_a1",
    "target_notional_ap_a2",
]

_VOL_TARGET = 0.50  # SS11.5/SS12.5: vol_scale = min(1.0, 0.50/sigma20)


def compute_vol_scale(sigma20: float) -> float:
    """``vol_scale = min(1.0, 0.50/sigma20)``. Never exceeds 1.0 (no
    leverage, SS11.5) — the ``min(1.0, ...)`` clamp is not optional."""
    if sigma20 <= 0.0:
        raise ValueError(f"sigma20 must be positive, got {sigma20!r}")
    return min(1.0, _VOL_TARGET / sigma20)


def ap_a1_base_slot_usd() -> float:
    """``base_slot = equity/32`` (SS11.5). ``$2000/32 == $62.50`` — read from
    the H2 seal's ``frozen_config`` identity component, never re-typed."""
    return sc.ap_a1_base_slot_usd()


def ap_a2_base_slot_usd(k: int) -> float:
    """``base_slot = equity/k`` (SS12.5) — ``equity`` read from the H2 seal,
    never re-typed."""
    if type(k) is not int or k <= 0:
        raise ValueError(f"k must be a positive int, got {k!r}")
    return sc.initial_equity_usd() / k


def target_notional_ap_a1(vol_scale: float) -> float:
    """``target_notional = base_slot x vol_scale`` (AP-A1, SS11.5). Not
    capped by available cash here — the cash constraint is applied
    separately, across all simultaneous candidates, by
    ``allocate_cash_constrained`` (AC12)."""
    return ap_a1_base_slot_usd() * vol_scale


def target_notional_ap_a2(*, k: int, vol_scale: float, available_cash: float) -> float:
    """``target_notional = min(available_cash, base_slot x vol_scale)``
    (AP-A2, SS12.5) — AP-A2 processes candidates strictly in rank order, one
    at a time, so the cash cap is applied inline per-candidate rather than
    via a separate simultaneous-tie-break allocator."""
    return min(available_cash, ap_a2_base_slot_usd(k) * vol_scale)


def meets_min_target_notional(target_notional: float) -> bool:
    """``target_notional >= $25`` (the SEALED floor, AC11) — below it, the
    candidate is rejected with ``MIN_TARGET_NOTIONAL`` and never reaches
    cash allocation."""
    return target_notional >= sc.min_strategy_target_usd()


@dataclass(frozen=True)
class AllocationOutcome:
    accepted: tuple[str, ...]  # symbols, in the order they were accepted
    rejected_insufficient_cash: tuple[str, ...]  # symbols, canonical order
    remaining_cash: float


def allocate_cash_constrained(
    candidates: Sequence[tuple[str, float, float]],
    *,
    available_cash: float,
) -> AllocationOutcome:
    """Greedily allocate ``available_cash`` across simultaneous entry
    candidates, ranked D/Score DESCENDING with symbol ASCENDING as the tie-
    break (AC12: "D 내림차순 → 동률 시 symbol 사전순"), never any other
    order.

    ``candidates`` is a sequence of ``(symbol, priority_value, target_notional)``
    — ``priority_value`` is ``D`` for AP-A1 (already filtered to
    ``D >= +threshold`` entrants) or unused by AP-A2 (which allocates
    one-at-a-time in ``target_notional_ap_a2`` instead, since it is already
    strictly rank-ordered before this function would ever be needed).

    GREEDY-CONTINUE, not stop-on-first-rejection (explicit, documented open
    choice — ROB-1061 adversarial-verification remediation, 2026-07-26):
    when the highest-priority candidate does not fit in ``available_cash``,
    this function does NOT stop there — it keeps walking the (already
    correctly ordered) list, so a LOWER-priority candidate whose smaller
    ``target_notional`` DOES fit is still funded. Example: cash=$100,
    AAA (D=.09, needs $900) ranked ahead of BBB (D=.02, needs $50) — AAA is
    rejected (doesn't fit), BBB is still accepted (fits in the full $100,
    since a rejected candidate never consumes cash). §11.5 specifies an
    ORDER for allocation ("D 내림차순 → symbol 사전순"), not a stop rule, so
    greedy-continue is a defensible reading that maximizes cash utilization
    within the mandated ordering — but it does change which entries actually
    fire versus a stop-on-first-rejection allocator, so it is pinned here as
    a deliberate choice (see
    ``test_allocation_uses_greedy_continue_not_stop_on_first_rejection``)
    rather than left as an undocumented accident of the implementation.
    """
    ordered = sorted(candidates, key=lambda c: (-c[1], c[0]))
    accepted: list[str] = []
    rejected: list[str] = []
    remaining = available_cash
    for symbol, _priority, target_notional in ordered:
        if target_notional <= remaining:
            accepted.append(symbol)
            remaining -= target_notional
        else:
            rejected.append(symbol)
    return AllocationOutcome(
        accepted=tuple(accepted),
        rejected_insufficient_cash=tuple(sorted(rejected)),
        remaining_cash=remaining,
    )


def available_cash(committed_notional_by_symbol: Mapping[str, float]) -> float:
    """The sealed initial equity minus the sum of entry-time committed
    notional for every currently-open position — the PnL-blind "available
    cash" figure every sizing decision in this module is measured against."""
    return sc.initial_equity_usd() - math.fsum(committed_notional_by_symbol.values())
