from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx

from app.core.exceptions import describe_exception
from app.services.order_proposals.errors import OrderProposalError
from app.services.order_proposals.target_order import TargetOrderSnapshot

SUPPORTED_TARGET_ACTIONS = frozenset(
    {
        ("kis_live", "equity_kr"),
        ("kis_live", "equity_us"),
        ("upbit", "crypto"),
    }
)


@dataclass(frozen=True)
class SubmitEvidence:
    outcome: Literal["found", "absent", "unknown"]
    broker_order_id: str | None = None
    broker_state: str | None = None
    reason: str | None = None


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def fetch_target_order(
    *,
    order_id: str,
    symbol: str,
    market: str,
    account_mode: str,
    now: datetime,
    history_fn: Callable[..., Any] | None = None,
) -> TargetOrderSnapshot:
    if (account_mode, market) not in SUPPORTED_TARGET_ACTIONS:
        raise OrderProposalError(
            f"target order lookup unsupported for {account_mode}/{market}"
        )
    if history_fn is None:
        from app.mcp_server.tooling.orders_history import get_order_history_impl

        history_fn = get_order_history_impl

    result = await _maybe_await(
        history_fn(
            symbol=symbol,
            status="all",
            order_id=order_id,
            market=market,
            limit=20,
            is_mock=False,
        )
    )
    errors = result.get("errors", [])
    if errors:
        raise OrderProposalError(f"target broker order lookup failed: {errors}")

    matches = [
        row
        for row in result.get("orders", [])
        if str(row.get("order_id") or "").strip() == order_id
    ]
    if len(matches) != 1:
        raise OrderProposalError("target broker order not found uniquely")
    return TargetOrderSnapshot.from_broker_order(matches[0], observed_at=now)


async def fetch_submit_evidence(
    *,
    identifier: str,
    account_mode: str,
    market: str,
    lookup_fn: Callable[..., Any] | None = None,
) -> SubmitEvidence:
    if (account_mode, market) != ("upbit", "crypto"):
        return SubmitEvidence(
            "unknown",
            reason=(
                f"submit evidence lookup unsupported for {account_mode}/{market}"
            ),
        )
    if lookup_fn is None:
        from app.services.brokers.upbit.orders import fetch_order_by_identifier

        lookup_fn = fetch_order_by_identifier

    try:
        order = await _maybe_await(lookup_fn(identifier))
        broker_order_id = str(order.get("uuid") or "").strip()
        broker_state = str(order.get("state") or "").strip()
        if not broker_order_id or not broker_state:
            return SubmitEvidence(
                "unknown",
                reason="broker lookup returned incomplete order evidence",
            )
        return SubmitEvidence("found", broker_order_id, broker_state)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return SubmitEvidence("absent")
        return SubmitEvidence("unknown", reason=describe_exception(exc))
    except Exception as exc:
        return SubmitEvidence("unknown", reason=describe_exception(exc))


async def cancel_target_order(
    *,
    order_id: str,
    symbol: str,
    market: str,
    account_mode: str,
    cancel_fn: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    if (account_mode, market) not in SUPPORTED_TARGET_ACTIONS:
        raise OrderProposalError(f"cancel unsupported for {account_mode}/{market}")
    if cancel_fn is None:
        from app.mcp_server.tooling.orders_modify_cancel import cancel_order_impl

        cancel_fn = cancel_order_impl

    return await _maybe_await(
        cancel_fn(
            order_id=order_id,
            symbol=symbol,
            market=market,
            is_mock=False,
        )
    )
