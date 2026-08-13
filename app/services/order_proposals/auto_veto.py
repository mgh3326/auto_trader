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
TossVetoReconcileFn = Callable[..., Any]

_CANCELLABLE_STATES = frozenset({"acked", "resting", "partially_filled", "unverified"})


async def reconcile_toss_auto_veto_terminal(
    *,
    order_id: str,
    symbol: str,
    market: str,
    account_mode: str,
) -> dict[str, Any]:
    """Reconcile a Toss veto only after the broker reports the original closed.

    Toss's cancel acknowledgement is a *request* and carries a replacement
    order id.  It is not terminal evidence for the original order.  This
    helper intentionally asks the evidence-backed Toss reconciler to close the
    original order in the ledger and only returns ``confirmed=True`` when the
    reconciliation result itself names that original order as cancelled.

    It is invoked by the runtime veto path, never by the offline fixtures.  A
    failure to find this proof deliberately leaves the proposal rung open and
    reports a cancellation failure to the operator.
    """
    if account_mode != "toss_live":
        return {"confirmed": True, "skipped": "not_toss_live"}
    toss_market = {"equity_kr": "kr", "equity_us": "us"}.get(market)
    if toss_market is None:
        return {
            "confirmed": False,
            "error": "toss_veto_market_unrecognized",
        }

    # Import inside the rare mutation path so normal proposal classification
    # stays isolated from the Toss MCP-tool registration modules.
    from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl

    report = await toss_reconcile_orders_impl(
        symbol=symbol,
        order_id=order_id,
        market=toss_market,
        dry_run=False,
        limit=10,
        project_proposal_rungs=False,
    )
    matches = [
        item
        for item in report.get("reconciled", [])
        if item.get("order_id") == order_id
        and item.get("local_status") == "cancelled"
        and item.get("action") in {"marked_cancelled", "booked", "noop_already_booked"}
    ]
    if not matches:
        # A previous reconcile may already have closed the original ledger row,
        # in which case it is no longer in the open-row worklist this targeted
        # pass scans.  Read only the evidence-stamped original `place` row;
        # a cancel replacement acknowledgement cannot satisfy this lookup.
        from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
        from app.services.toss_live_order_ledger_service import (
            TossLiveOrderLedgerService,
        )

        async with _order_session_factory()() as db:
            terminal_status = await TossLiveOrderLedgerService(
                db
            ).reconciled_terminal_status_for_place_order(broker_order_id=order_id)
        if terminal_status == "cancelled":
            return {
                "confirmed": True,
                "reconciled_order_count": 1,
                "confirmed_from": "existing_terminal_ledger_reconcile",
                "error": None,
            }
    return {
        "confirmed": bool(matches),
        "reconciled_order_count": len(matches),
        "error": None if matches else "toss_terminal_reconcile_unconfirmed",
    }


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
    toss_reconcile_fn: TossVetoReconcileFn = reconcile_toss_auto_veto_terminal,
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
            # `fetch_fn` is the first required broker-terminal observation.
            # Toss requires a second, independent ledger reconciliation for
            # the *original* broker id before a local rung may say cancelled.
            reconcile: dict[str, Any] | None = None
            if group.account_mode == "toss_live":
                try:
                    raw_reconcile = await toss_reconcile_fn(
                        order_id=rung.broker_order_id,
                        symbol=group.symbol,
                        market=group.market,
                        account_mode=group.account_mode,
                    )
                    reconcile = (
                        raw_reconcile
                        if isinstance(raw_reconcile, dict)
                        else {"confirmed": False, "error": "invalid_reconcile_result"}
                    )
                except Exception as exc:  # noqa: BLE001 - evidence is mandatory
                    reconcile = {
                        "confirmed": False,
                        "error": str(exc) or exc.__class__.__name__,
                    }
                if reconcile.get("confirmed") is not True:
                    outcomes.append(
                        {
                            "rung_index": rung.rung_index,
                            "result": "cancel_failed",
                            "broker_status": status,
                            "terminal_confirmation": "toss_ledger_reconcile_required",
                            "reconcile": reconcile,
                            "error": "toss_terminal_reconcile_unconfirmed",
                        }
                    )
                    continue
            await service.record_cancelled(
                group.proposal_id,
                rung.rung_index,
                broker_order_id=rung.broker_order_id,
                now=now,
            )
            outcome: dict[str, Any] = {
                "rung_index": rung.rung_index,
                "result": "cancelled",
                "terminal_confirmation": "broker_cancelled",
            }
            if reconcile is not None:
                outcome["reconcile"] = reconcile
                outcome["terminal_confirmation"] = (
                    "broker_terminal_and_toss_ledger_reconciled"
                )
            outcomes.append(outcome)
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
    "TossVetoReconcileFn",
    "acquire_auto_veto_locks",
    "cancel_auto_submitted_rungs",
    "reconcile_toss_auto_veto_terminal",
]
