"""Guarded, one-time local ledger repair for the ROB-837 Upbit incident.

This command only reads the broker order and updates the local proposal ledger after
all incident-specific guards pass. It never resubmits or cancels the live order.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.services.brokers.upbit import fetch_order_detail
from app.services.order_proposals.service import OrderProposalsService

_INCIDENT_PROPOSAL_PREFIX = "b81ffd0e"
_INCIDENT_BROKER_ORDER_PREFIX = "35bee07f"
_EXPECTED_BROKER_FIELDS = {
    "market": "KRW-BTC",
    "state": "wait",
    "side": "bid",
    "ord_type": "limit",
}


def _serialize_timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _snapshot(group: OrderProposal, rung: OrderProposalRung) -> dict[str, Any]:
    return {
        "state": rung.state,
        "broker_order_id": rung.broker_order_id,
        "idempotency_key": rung.idempotency_key,
        "void_reason": rung.void_reason,
        "validated_at": _serialize_timestamp(rung.validated_at),
        "updated_at": _serialize_timestamp(rung.updated_at),
        "group_lifecycle_state": group.lifecycle_state,
        "group_updated_at": _serialize_timestamp(group.updated_at),
    }


def _validate_incident_ids(proposal_id: uuid.UUID, broker_order_id: str) -> None:
    if proposal_id.hex[:8] != _INCIDENT_PROPOSAL_PREFIX:
        raise ValueError("proposal id is not the ROB-837 incident")
    if not broker_order_id.startswith(_INCIDENT_BROKER_ORDER_PREFIX):
        raise ValueError("broker order id is not the ROB-837 incident")


def _validate_broker_order(
    broker_order: dict[str, Any],
    *,
    broker_order_id: str,
    group: OrderProposal,
    rung: OrderProposalRung,
) -> None:
    if broker_order.get("uuid") != broker_order_id:
        raise ValueError("broker order uuid does not match requested broker order id")
    for field, expected in _EXPECTED_BROKER_FIELDS.items():
        if broker_order.get(field) != expected:
            raise ValueError(f"broker order {field} does not match ROB-837 evidence")
    if not str(broker_order.get("identifier") or "").strip():
        raise ValueError("broker order identifier is empty")
    if group.symbol != broker_order.get("market"):
        raise ValueError("proposal symbol does not match broker order market")
    if group.side != rung.side:
        raise ValueError("proposal side does not match proposal rung side")
    expected_broker_side = {"buy": "bid", "sell": "ask"}.get(rung.side)
    if expected_broker_side != broker_order.get("side"):
        raise ValueError("proposal side does not match broker order side")
    if group.order_type != broker_order.get("ord_type"):
        raise ValueError("proposal order type does not match broker order type")
    if Decimal(str(broker_order.get("price"))) != Decimal(str(rung.limit_price)):
        raise ValueError("broker order price does not match proposal rung")
    if Decimal(str(broker_order.get("volume"))) != Decimal(str(rung.quantity)):
        raise ValueError("broker order volume does not match proposal rung")


async def _get_locked_group_and_rung(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    rung_index: int,
) -> tuple[OrderProposal, OrderProposalRung, list[OrderProposalRung]]:
    group = (
        await session.execute(
            select(OrderProposal)
            .where(OrderProposal.proposal_id == proposal_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if group is None:
        raise ValueError("proposal was not found")
    if group.account_mode != "upbit" or group.market != "crypto":
        raise ValueError("proposal is not an Upbit crypto proposal")

    rungs = list(
        (
            await session.execute(
                select(OrderProposalRung)
                .where(OrderProposalRung.proposal_pk == group.id)
                .order_by(OrderProposalRung.rung_index)
                .with_for_update()
            )
        ).scalars()
    )
    rung = next((row for row in rungs if row.rung_index == rung_index), None)
    if rung is None:
        raise ValueError("proposal rung was not found")
    if rung.state != "rejected":
        raise ValueError("proposal rung state is not rejected")
    return group, rung, rungs


async def _assert_no_other_broker_binding(
    session: AsyncSession,
    *,
    rung: OrderProposalRung,
    broker_order_id: str,
) -> None:
    duplicate = (
        (
            await session.execute(
                select(OrderProposalRung)
                .where(
                    OrderProposalRung.broker_order_id == broker_order_id,
                    OrderProposalRung.id != rung.id,
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )
    if duplicate is not None:
        raise ValueError("broker order id is already bound to another rung")


async def repair_incident(
    session: AsyncSession,
    *,
    proposal_id: uuid.UUID,
    rung_index: int,
    broker_order_id: str,
    commit: bool,
    fetch_order_fn: Callable[[str], Awaitable[dict[str, Any]]] = fetch_order_detail,
) -> dict[str, Any]:
    """Return the verified repair diff, applying it only when ``commit`` is true."""
    try:
        _validate_incident_ids(proposal_id, broker_order_id)
        broker_order = await fetch_order_fn(broker_order_id)
        group, rung, rungs = await _get_locked_group_and_rung(
            session,
            proposal_id=proposal_id,
            rung_index=rung_index,
        )
        _validate_broker_order(
            broker_order,
            broker_order_id=broker_order_id,
            group=group,
            rung=rung,
        )
        await _assert_no_other_broker_binding(
            session,
            rung=rung,
            broker_order_id=broker_order_id,
        )

        now = datetime.now(UTC)
        before = _snapshot(group, rung)
        rung.state = "resting"
        rung.broker_order_id = broker_order_id
        rung.idempotency_key = broker_order["identifier"]
        rung.void_reason = None
        rung.validated_at = now
        rung.updated_at = now
        group.lifecycle_state = OrderProposalsService._recompute_group_state(rungs)
        group.updated_at = now
        after = _snapshot(group, rung)
        result = {
            "mode": "commit" if commit else "dry-run",
            "proposal_id": str(proposal_id),
            "rung_index": rung_index,
            "broker_order_id": broker_order_id,
            "evidence": broker_order,
            "before": before,
            "after": after,
        }
        if not commit:
            await session.rollback()
            return result

        await session.commit()
        return result
    except Exception:
        await session.rollback()
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Repair only the ROB-837 local proposal ledger after exact read-only "
            "broker evidence. Dry-run is the default; --commit is the explicit "
            "mutation gate. Never resubmits or cancels the live order."
        )
    )
    parser.add_argument(
        "--proposal-id",
        required=True,
        type=uuid.UUID,
        help="full ROB-837 proposal UUID beginning with b81ffd0e",
    )
    parser.add_argument(
        "--broker-order-id",
        required=True,
        help="full ROB-837 broker order UUID beginning with 35bee07f",
    )
    parser.add_argument("--rung-index", type=int, default=0)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="apply the verified local ledger repair; default is dry-run",
    )
    return parser.parse_args(argv)


async def _main() -> int:
    args = parse_args()
    async with AsyncSessionLocal() as session:
        result = await repair_incident(
            session,
            proposal_id=args.proposal_id,
            rung_index=args.rung_index,
            broker_order_id=args.broker_order_id,
            commit=args.commit,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
