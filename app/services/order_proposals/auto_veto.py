"""Shared broker-cancel convergence for ROB-871 auto-submission vetoes."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.order_proposals.service import OrderProposalsService

TargetCancelFn = Callable[..., Any]
TargetFetchFn = Callable[..., Any]

_CANCELLABLE_STATES = frozenset({"acked", "resting", "partially_filled", "unverified"})


async def acquire_auto_veto_locks(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rungs: Sequence[OrderProposalRung],
) -> None:
    """Lock broker targets in stable order before any proposal row lock."""
    broker_order_ids = sorted(
        {
            str(rung.broker_order_id)
            for rung in rungs
            if rung.state in _CANCELLABLE_STATES and rung.broker_order_id
        }
    )
    for broker_order_id in broker_order_ids:
        await service.acquire_broker_order_mutation_lock(group, broker_order_id)


async def cancel_auto_submitted_rungs(
    *,
    service: OrderProposalsService,
    group: OrderProposal,
    rungs: Sequence[OrderProposalRung],
    now: datetime,
    cancel_fn: TargetCancelFn,
    fetch_fn: TargetFetchFn,
) -> list[dict[str, Any]]:
    """Request cancel, then converge each rung only from fresh broker status."""
    outcomes: list[dict[str, Any]] = []
    for rung in rungs:
        if rung.state == "filled":
            outcomes.append({"rung_index": rung.rung_index, "result": "filled"})
            continue
        if rung.state == "cancelled":
            outcomes.append({"rung_index": rung.rung_index, "result": "cancelled"})
            continue
        if rung.state not in _CANCELLABLE_STATES or not rung.broker_order_id:
            outcomes.append(
                {"rung_index": rung.rung_index, "result": "not_cancellable"}
            )
            continue

        cancel_error: str | None = None
        try:
            cancel_result = await cancel_fn(
                order_id=rung.broker_order_id,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
            )
            if (
                not isinstance(cancel_result, dict)
                or cancel_result.get("success") is not True
            ):
                cancel_error = (
                    str(cancel_result.get("error") or "cancel_rejected")
                    if isinstance(cancel_result, dict)
                    else "cancel_rejected"
                )
        except Exception as exc:  # noqa: BLE001 - confirm after ambiguity
            cancel_error = str(exc)

        try:
            snapshot = await fetch_fn(
                order_id=rung.broker_order_id,
                symbol=group.symbol,
                market=group.market,
                account_mode=group.account_mode,
                now=now,
            )
            status = snapshot.status
        except Exception as exc:  # noqa: BLE001 - persist explicit uncertainty
            status = None
            cancel_error = cancel_error or str(exc)

        if status == "cancelled":
            await service.record_cancelled(
                group.proposal_id,
                rung.rung_index,
                broker_order_id=rung.broker_order_id,
                now=now,
            )
            outcomes.append({"rung_index": rung.rung_index, "result": "cancelled"})
        elif status == "filled":
            await service.transition_rung(
                group.proposal_id,
                rung.rung_index,
                new_state="filled",
                broker_order_id=rung.broker_order_id,
                filled_qty=Decimal(rung.quantity),
                validated_at=now,
                updated_at=now,
            )
            outcomes.append({"rung_index": rung.rung_index, "result": "filled"})
        else:
            outcomes.append(
                {
                    "rung_index": rung.rung_index,
                    "result": "cancel_failed",
                    "broker_status": status,
                    "error": cancel_error,
                }
            )
    return outcomes


__all__ = [
    "TargetCancelFn",
    "TargetFetchFn",
    "acquire_auto_veto_locks",
    "cancel_auto_submitted_rungs",
]
