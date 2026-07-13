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


@dataclass(frozen=True)
class OperatorVoidEvidence:
    outcome: Literal["found", "absent", "unknown"]
    lookup_scope: str
    broker_order_id: str | None = None
    broker_state: str | None = None
    reason: str | None = None


_TOSS_CLOSED_PAGE_CAP = 20


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
            reason=(f"submit evidence lookup unsupported for {account_mode}/{market}"),
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


async def fetch_operator_void_evidence(
    *,
    account_mode: str,
    market: str,
    symbol: str,
    rungs: list[Any],
    now: datetime,
    toss_client: Any | None = None,
    history_fn: Callable[..., Any] | None = None,
    upbit_identifier_lookup_fn: Callable[..., Any] | None = None,
    upbit_order_lookup_fn: Callable[..., Any] | None = None,
) -> dict[int, OperatorVoidEvidence]:
    """Prove broker-order absence for explicit operator voids.

    Toss is scanned once for OPEN orders and through a bounded CLOSED window.
    An incomplete scan or any broker exception is ``unknown`` so callers fail
    closed instead of treating missing evidence as cancellation evidence.
    """
    if account_mode == "kis_live" and market in {"equity_kr", "equity_us"}:
        if history_fn is None:
            from app.mcp_server.tooling.orders_history import get_order_history_impl

            history_fn = get_order_history_impl
        lookup_days = max(
            1,
            (now.date() - min(rung.created_at for rung in rungs).date()).days + 1,
        )
        scope = f"kis order history status=all days={lookup_days}"
        try:
            history = await _maybe_await(
                history_fn(
                    symbol=symbol,
                    status="all",
                    market=market,
                    days=lookup_days,
                    limit=-1,
                    is_mock=False,
                )
            )
            if history.get("truncated"):
                reason = "broker history lookup truncated"
                return {
                    rung.rung_index: OperatorVoidEvidence(
                        "unknown", scope, reason=reason
                    )
                    for rung in rungs
                }
            if history.get("errors"):
                reason = f"broker history errors: {history['errors']}"
                return {
                    rung.rung_index: OperatorVoidEvidence(
                        "unknown", scope, reason=reason
                    )
                    for rung in rungs
                }
            orders = history.get("orders", [])
            result = {}
            for rung in rungs:
                order_id = str(rung.broker_order_id or "").strip()
                match = next(
                    (
                        order
                        for order in orders
                        if str(order.get("order_id") or "").strip() == order_id
                    ),
                    None,
                )
                if match is not None:
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "found",
                        scope,
                        broker_order_id=order_id,
                        broker_state=str(match.get("status") or "unknown"),
                    )
                elif order_id and market == "equity_kr":
                    result[rung.rung_index] = OperatorVoidEvidence("absent", scope)
                elif order_id:
                    # The shared KIS US history adapter can return partial
                    # results when an exchange's open-order inquiry fails.
                    # A match is still positive evidence, but an empty result
                    # is not complete enough to prove absence.
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "unknown",
                        scope,
                        reason="KIS US history absence cannot be proven complete",
                    )
                else:
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "unknown",
                        scope,
                        reason="KIS rung has no broker_order_id",
                    )
            return result
        except Exception as exc:
            return {
                rung.rung_index: OperatorVoidEvidence(
                    "unknown", scope, reason=describe_exception(exc)
                )
                for rung in rungs
            }

    if account_mode == "upbit" and market == "crypto":
        if upbit_identifier_lookup_fn is None or upbit_order_lookup_fn is None:
            from app.services.brokers.upbit.orders import (
                fetch_order_by_identifier,
                fetch_order_detail,
            )

            upbit_identifier_lookup_fn = (
                upbit_identifier_lookup_fn or fetch_order_by_identifier
            )
            upbit_order_lookup_fn = upbit_order_lookup_fn or fetch_order_detail
        result = {}
        for rung in rungs:
            identifier = str(rung.idempotency_key or "").strip()
            order_id = str(rung.broker_order_id or "").strip()
            key_kind = "identifier" if identifier else "broker_order_id"
            key = identifier or order_id
            scope = f"upbit GET /order by {key_kind}"
            if not key:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason="rung has no broker lookup identifier"
                )
                continue
            lookup_fn = (
                upbit_identifier_lookup_fn if identifier else upbit_order_lookup_fn
            )
            try:
                order = await _maybe_await(lookup_fn(key))
                found_id = str(order.get("uuid") or "").strip()
                state = str(order.get("state") or "").strip()
                if not found_id or not state:
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "unknown",
                        scope,
                        reason="broker lookup returned incomplete order evidence",
                    )
                else:
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "found", scope, found_id, state
                    )
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    result[rung.rung_index] = OperatorVoidEvidence("absent", scope)
                else:
                    result[rung.rung_index] = OperatorVoidEvidence(
                        "unknown", scope, reason=describe_exception(exc)
                    )
            except Exception as exc:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason=describe_exception(exc)
                )
        return result

    if account_mode != "toss_live" or market not in {"equity_kr", "equity_us"}:
        scope = f"unsupported {account_mode}/{market}"
        return {
            rung.rung_index: OperatorVoidEvidence(
                "unknown", scope, reason="operator void lookup unsupported"
            )
            for rung in rungs
        }

    from app.core.timezone import KST
    from app.services.brokers.toss.client import TossReadClient

    window_from = (
        min(rung.created_at for rung in rungs).astimezone(KST).date().isoformat()
    )
    window_to = now.astimezone(KST).date().isoformat()
    owns_client = toss_client is None
    client = toss_client or TossReadClient.from_settings()
    orders: list[Any] = []
    closed_pages = 0
    incomplete_reason: str | None = None
    try:
        open_page = await client.list_orders(status="OPEN", symbol=symbol)
        orders.extend(open_page.orders)

        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            page = await client.list_orders(
                status="CLOSED",
                symbol=symbol,
                from_date=window_from,
                to_date=window_to,
                cursor=cursor,
                limit=100,
            )
            orders.extend(page.orders)
            closed_pages += 1
            if not page.has_next:
                break
            if not page.next_cursor:
                incomplete_reason = "CLOSED order scan missing next cursor"
                break
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                incomplete_reason = "CLOSED order scan repeated cursor"
                break
            if closed_pages >= _TOSS_CLOSED_PAGE_CAP:
                incomplete_reason = "CLOSED order scan page cap reached"
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

        scope = (
            "toss GET /orders OPEN + CLOSED "
            f"{window_from}..{window_to} pages={closed_pages} "
            f"complete={str(incomplete_reason is None).lower()}"
        )
        result: dict[int, OperatorVoidEvidence] = {}
        for rung in rungs:
            broker_order_id = str(rung.broker_order_id or "").strip()
            idempotency_key = str(rung.idempotency_key or "").strip()
            match = next(
                (
                    order
                    for order in orders
                    if (broker_order_id and str(order.order_id) == broker_order_id)
                    or (
                        idempotency_key
                        and str(getattr(order, "client_order_id", "") or "")
                        == idempotency_key
                    )
                ),
                None,
            )
            if match is not None:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "found",
                    scope,
                    broker_order_id=str(match.order_id),
                    broker_state=str(match.status),
                )
            elif not broker_order_id and not idempotency_key:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason="rung has no broker lookup identifier"
                )
            elif (
                idempotency_key
                and not broker_order_id
                and any(
                    getattr(order, "client_order_id", None) is None for order in orders
                )
            ):
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown",
                    scope,
                    reason="broker order list omitted clientOrderId",
                )
            elif incomplete_reason is not None:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason=incomplete_reason
                )
            else:
                result[rung.rung_index] = OperatorVoidEvidence("absent", scope)
        return result
    except Exception as exc:
        scope = f"toss GET /orders OPEN + CLOSED {window_from}..{window_to} incomplete"
        return {
            rung.rung_index: OperatorVoidEvidence(
                "unknown", scope, reason=describe_exception(exc)
            )
            for rung in rungs
        }
    finally:
        if owns_client:
            await client.aclose()


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
