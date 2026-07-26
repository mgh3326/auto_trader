"""ROB-1061 H3 (SS11.5, SS12.5) — entry-time vol sizing, the $25
``MIN_TARGET_NOTIONAL`` floor (read from the H2 seal, never hardcoded), and
the D/Score-descending, symbol-ascending cash-constrained allocation
tie-break (AC12).

``INITIAL_EQUITY_USD`` (=2000.0) and ``AP_A1_BASE_SLOT_DIVISOR`` (=32) are
fixed STRATEGY-FORMULA constants straight from the Run A preregistration
(SS11.5: "초기 equity $2,000 고정. base_slot = equity/32 = $62.50") — H2's
seal never captures these (it seals only the 4 MEASURED execution
parameters: universe, spread census, paper fee, frozen basis cap, plus the
config domain and gate thresholds; H2's own ``identity.py`` embeds the same
2000/62.50 literals for its ROB-846 identity component, consistent with
this module). The $25 floor, by contrast, IS one of H2's sealed values
(``run_status.min_strategy_target_usd``) and is read via
``seal_consumption.min_strategy_target_usd`` here, never re-typed.

PnL-blind by construction: "available cash" is always ``INITIAL_EQUITY_USD``
minus the sum of entry-time COMMITTED notional for currently-open positions
— never a running mark-to-market balance, never adjusted for price moves or
realized/unrealized P&L (positions are never resized/rebalanced while held,
SS11.5/SS12.5 "보유 중 리밸런스 없음"). No ``pnl``/``return``/``forward_*``/
``exit_price`` field anywhere in this module.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import seal_consumption as sc

__all__ = [
    "AP_A1_BASE_SLOT_DIVISOR",
    "INITIAL_EQUITY_USD",
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

# SS11.5 — fixed strategy-formula constants, not part of H2's measured-
# execution-parameter seal (see module docstring).
INITIAL_EQUITY_USD = 2000.0
AP_A1_BASE_SLOT_DIVISOR = 32
_VOL_TARGET = 0.50  # SS11.5/SS12.5: vol_scale = min(1.0, 0.50/sigma20)


def compute_vol_scale(sigma20: float) -> float:
    """``vol_scale = min(1.0, 0.50/sigma20)``. Never exceeds 1.0 (no
    leverage, SS11.5) — the ``min(1.0, ...)`` clamp is not optional."""
    if sigma20 <= 0.0:
        raise ValueError(f"sigma20 must be positive, got {sigma20!r}")
    return min(1.0, _VOL_TARGET / sigma20)


def ap_a1_base_slot_usd() -> float:
    """``base_slot = equity/32`` (SS11.5). ``$2000/32 == $62.50``."""
    return INITIAL_EQUITY_USD / AP_A1_BASE_SLOT_DIVISOR


def ap_a2_base_slot_usd(k: int) -> float:
    """``base_slot = equity/k`` (SS12.5)."""
    if type(k) is not int or k <= 0:
        raise ValueError(f"k must be a positive int, got {k!r}")
    return INITIAL_EQUITY_USD / k


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
    """``INITIAL_EQUITY_USD`` minus the sum of entry-time committed notional
    for every currently-open position — the PnL-blind "available cash"
    figure every sizing decision in this module is measured against."""
    return INITIAL_EQUITY_USD - math.fsum(committed_notional_by_symbol.values())
