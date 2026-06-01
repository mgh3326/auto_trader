"""Pure holdings-delta reconciler for KIS mock orders (ROB-102).

Read-only / decision-support only. This module must not import broker,
DB, KIS client, ORM, or sqlalchemy code. Callers collect ledger rows and
holdings snapshots and pass plain dataclasses.

Output `next_state` values are always one of the ROB-100
`OrderLifecycleState` literals. Internal "fine-grained" meaning is encoded
in `reason_code`.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from app.schemas.execution_contracts import OrderLifecycleState

ReasonCode = Literal[
    "fill_detected",
    "partial_fill_detected",
    "pending_unconfirmed",
    "stale_unconfirmed",
    "position_reconciled",
    "holdings_mismatch",
    "holdings_snapshot_missing",
    "baseline_missing",
]


@dataclass(frozen=True, slots=True)
class LedgerOrderInput:
    ledger_id: int
    symbol: str
    side: Literal["buy", "sell"]
    ordered_qty: Decimal
    lifecycle_state: OrderLifecycleState
    holdings_baseline_qty: Decimal | None
    accepted_at: datetime
    price: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class HoldingsSnapshot:
    symbol: str
    quantity: Decimal
    taken_at: datetime


@dataclass(frozen=True, slots=True)
class ReconcilerThresholds:
    pending_threshold_sec: int = 60
    stale_threshold_sec: int = 1800


@dataclass(frozen=True, slots=True)
class LifecycleTransitionProposal:
    ledger_id: int
    symbol: str
    prior_state: OrderLifecycleState
    next_state: OrderLifecycleState
    reason_code: ReasonCode
    observed_holdings_qty: Decimal | None
    observed_delta: Decimal | None
    attributed_fill_qty: Decimal | None = None


@dataclass(frozen=True, slots=True)
class DeltaFillResult:
    verdict: Literal["filled", "partial", "none"]
    filled_qty: Decimal  # magnitude in the order's direction, clamped to ordered_qty
    delta: Decimal


def classify_fill_by_delta(
    *,
    side: Literal["buy", "sell"],
    ordered_qty: Decimal,
    baseline_qty: Decimal,
    observed_qty: Decimal,
) -> DeltaFillResult:
    """Pure delta -> fill decision shared by the periodic reconciler and the
    synchronous confirm path (ROB-341).

    ``filled_qty`` is the magnitude of the position change in the order's
    direction, clamped to ``ordered_qty``. A delta in the wrong direction
    (holdings dropped on a buy / rose on a sell) yields ``none`` — never a fill.
    """
    delta = observed_qty - baseline_qty
    directional = delta if side == "buy" else -delta
    if directional <= 0:
        return DeltaFillResult("none", Decimal("0"), delta)
    filled = directional if directional < ordered_qty else ordered_qty
    verdict = "filled" if directional >= ordered_qty else "partial"
    return DeltaFillResult(verdict, filled, delta)


_TERMINAL: frozenset[str] = frozenset({"reconciled", "failed", "stale"})
_RECONCILABLE_INPUTS: frozenset[str] = frozenset({"accepted", "pending", "fill"})


def classify_orders(
    *,
    orders: Sequence[LedgerOrderInput],
    holdings: Mapping[str, HoldingsSnapshot],
    thresholds: ReconcilerThresholds,
    now: datetime,
) -> list[LifecycleTransitionProposal]:
    proposals: list[LifecycleTransitionProposal] = []
    for order in orders:
        if order.lifecycle_state in _TERMINAL:
            continue
        if order.lifecycle_state not in _RECONCILABLE_INPUTS:
            # planned/previewed/submitted/anomaly are out of scope here.
            continue

        if order.holdings_baseline_qty is None:
            proposals.append(
                LifecycleTransitionProposal(
                    ledger_id=order.ledger_id,
                    symbol=order.symbol,
                    prior_state=order.lifecycle_state,
                    next_state="anomaly",
                    reason_code="baseline_missing",
                    observed_holdings_qty=None,
                    observed_delta=None,
                )
            )
            continue

        snapshot = holdings.get(order.symbol)
        if snapshot is None:
            proposals.append(
                LifecycleTransitionProposal(
                    ledger_id=order.ledger_id,
                    symbol=order.symbol,
                    prior_state=order.lifecycle_state,
                    next_state="anomaly",
                    reason_code="holdings_snapshot_missing",
                    observed_holdings_qty=None,
                    observed_delta=None,
                )
            )
            continue

        delta = snapshot.quantity - order.holdings_baseline_qty

        if order.lifecycle_state == "fill":
            expected = order.ordered_qty if order.side == "buy" else -order.ordered_qty
            if (order.side == "buy" and delta >= expected) or (
                order.side == "sell" and delta <= expected
            ):
                proposals.append(
                    LifecycleTransitionProposal(
                        ledger_id=order.ledger_id,
                        symbol=order.symbol,
                        prior_state=order.lifecycle_state,
                        next_state="reconciled",
                        reason_code="position_reconciled",
                        observed_holdings_qty=snapshot.quantity,
                        observed_delta=delta,
                    )
                )
            else:
                proposals.append(
                    LifecycleTransitionProposal(
                        ledger_id=order.ledger_id,
                        symbol=order.symbol,
                        prior_state=order.lifecycle_state,
                        next_state="anomaly",
                        reason_code="holdings_mismatch",
                        observed_holdings_qty=snapshot.quantity,
                        observed_delta=delta,
                    )
                )
            continue

        # accepted / pending paths — delegate the delta decision to the kernel.
        decision = classify_fill_by_delta(
            side=order.side,
            ordered_qty=order.ordered_qty,
            baseline_qty=order.holdings_baseline_qty,
            observed_qty=snapshot.quantity,
        )
        if decision.verdict == "filled":
            next_state, reason = "fill", "fill_detected"
        elif decision.verdict == "partial":
            next_state, reason = "fill", "partial_fill_detected"
        else:
            next_state, reason = _pending_or_stale(order, now, thresholds)

        proposals.append(
            LifecycleTransitionProposal(
                ledger_id=order.ledger_id,
                symbol=order.symbol,
                prior_state=order.lifecycle_state,
                next_state=next_state,
                reason_code=reason,
                observed_holdings_qty=snapshot.quantity,
                observed_delta=delta,
            )
        )
    return proposals


def _pending_or_stale(
    order: LedgerOrderInput,
    now: datetime,
    thresholds: ReconcilerThresholds,
) -> tuple[OrderLifecycleState, ReasonCode]:
    age = (now - order.accepted_at).total_seconds()
    if age >= thresholds.stale_threshold_sec:
        return "stale", "stale_unconfirmed"
    return "pending", "pending_unconfirmed"


__all__ = [
    "DeltaFillResult",
    "HoldingsSnapshot",
    "LedgerOrderInput",
    "LifecycleTransitionProposal",
    "ReasonCode",
    "ReconcilerThresholds",
    "classify_fill_by_delta",
    "classify_orders",
]
