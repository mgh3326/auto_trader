from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
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
_TOSS_VOID_WINDOW_PAD = timedelta(hours=24)


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal order evidence") from None
    if not parsed.is_finite():
        raise ValueError("non-finite decimal order evidence")
    return parsed


def _toss_rung_window(
    rung: Any, *, valid_until: datetime | None
) -> tuple[datetime, datetime]:
    start = rung.created_at - _TOSS_VOID_WINDOW_PAD
    attempt_end = max(
        value for value in (valid_until, rung.updated_at) if value is not None
    )
    return start, attempt_end + _TOSS_VOID_WINDOW_PAD


def _aware_datetime(value: Any, *, field: str) -> datetime:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{field} must be a timezone-aware datetime")
    return value


def _parse_aware_iso_datetime(value: Any, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        raise ValueError(f"invalid {field} order evidence") from None
    return _aware_datetime(parsed, field=field)


def _normalize_order_text(value: Any) -> str:
    return str(value or "").strip().upper()


def _toss_lookup_scope(
    *,
    window_from: str,
    window_to: str,
    rung_window: tuple[datetime, datetime],
    closed_pages: int,
    complete: bool,
    combination_matches: int,
) -> str:
    rung_start, rung_end = rung_window
    return (
        "toss GET /orders OPEN + CLOSED "
        f"scan_kst={window_from}..{window_to} "
        f"rung_window={rung_start.isoformat()}..{rung_end.isoformat()} "
        f"closed_pages={closed_pages} complete={str(complete).lower()} "
        f"combination_matches={combination_matches}"
    )


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
    valid_until: datetime | None = None,
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

    window_from: str | None = None
    window_to: str | None = None
    rung_windows: dict[int, tuple[datetime, datetime]] = {}
    owns_client = toss_client is None
    client = toss_client
    orders: list[Any] = []
    closed_pages = 0
    incomplete_reason: str | None = None
    try:
        _aware_datetime(now, field="now")
        if valid_until is not None:
            _aware_datetime(valid_until, field="valid_until")
        for rung in rungs:
            _aware_datetime(rung.created_at, field="rung.created_at")
            if rung.updated_at is not None:
                _aware_datetime(rung.updated_at, field="rung.updated_at")
            rung_windows[rung.rung_index] = _toss_rung_window(
                rung, valid_until=valid_until
            )

        scan_start = min(start for start, _end in rung_windows.values())
        scan_end = max(end for _start, end in rung_windows.values())
        window_from = scan_start.astimezone(KST).date().isoformat()
        window_to = scan_end.astimezone(KST).date().isoformat()

        client = client or TossReadClient.from_settings()
        open_page = await client.list_orders(
            status="OPEN",
            symbol=symbol,
            from_date=window_from,
            to_date=window_to,
        )
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
            if closed_pages >= _TOSS_CLOSED_PAGE_CAP:
                incomplete_reason = "CLOSED order scan page cap reached"
                break
            if not page.next_cursor:
                incomplete_reason = "CLOSED order scan missing next cursor"
                break
            if page.next_cursor == cursor or page.next_cursor in seen_cursors:
                incomplete_reason = "CLOSED order scan repeated cursor"
                break
            seen_cursors.add(page.next_cursor)
            cursor = page.next_cursor

        result: dict[int, OperatorVoidEvidence] = {}
        for rung in rungs:
            rung_window = rung_windows[rung.rung_index]
            broker_order_id = str(rung.broker_order_id or "").strip()
            idempotency_key = str(rung.idempotency_key or "").strip()
            identifier_match = next(
                (
                    order
                    for order in orders
                    if (
                        broker_order_id
                        and str(getattr(order, "order_id", "") or "").strip()
                        == broker_order_id
                    )
                    or (
                        idempotency_key
                        and str(
                            getattr(order, "client_order_id", "") or ""
                        ).strip()
                        == idempotency_key
                    )
                ),
                None,
            )
            if identifier_match is not None:
                scope = _toss_lookup_scope(
                    window_from=window_from,
                    window_to=window_to,
                    rung_window=rung_window,
                    closed_pages=closed_pages,
                    complete=incomplete_reason is None,
                    combination_matches=0,
                )
                result[rung.rung_index] = OperatorVoidEvidence(
                    "found",
                    scope,
                    broker_order_id=str(identifier_match.order_id),
                    broker_state=str(identifier_match.status),
                )
                continue

            malformed_reason: str | None = None
            combination_match: Any | None = None
            rung_quantity: Decimal | None = None
            rung_price: Decimal | None = None
            normalized_symbol = _normalize_order_text(symbol)
            normalized_side = _normalize_order_text(rung.side)
            if not normalized_symbol or not normalized_side:
                malformed_reason = "invalid symbol or side order evidence"
            else:
                try:
                    rung_quantity = _finite_decimal(rung.quantity)
                    rung_price = _finite_decimal(rung.limit_price)
                    if rung_quantity is None:
                        raise ValueError("invalid decimal order evidence")
                except (TypeError, ValueError) as exc:
                    malformed_reason = describe_exception(exc)

            if malformed_reason is None:
                for order in orders:
                    if (
                        _normalize_order_text(getattr(order, "symbol", None))
                        != normalized_symbol
                        or _normalize_order_text(getattr(order, "side", None))
                        != normalized_side
                    ):
                        continue
                    try:
                        order_quantity = _finite_decimal(
                            getattr(order, "quantity", None)
                        )
                        order_price = _finite_decimal(getattr(order, "price", None))
                        if order_quantity is None:
                            raise ValueError("invalid decimal order evidence")
                    except (TypeError, ValueError) as exc:
                        malformed_reason = describe_exception(exc)
                        continue
                    if order_quantity != rung_quantity or order_price != rung_price:
                        continue
                    try:
                        ordered_at = _parse_aware_iso_datetime(
                            getattr(order, "ordered_at", None), field="ordered_at"
                        )
                    except ValueError as exc:
                        malformed_reason = describe_exception(exc)
                        continue
                    if rung_window[0] <= ordered_at <= rung_window[1]:
                        combination_match = order
                        break

            scope = _toss_lookup_scope(
                window_from=window_from,
                window_to=window_to,
                rung_window=rung_window,
                closed_pages=closed_pages,
                complete=incomplete_reason is None,
                combination_matches=int(combination_match is not None),
            )
            if combination_match is not None:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "found",
                    scope,
                    broker_order_id=str(combination_match.order_id),
                    broker_state=str(combination_match.status),
                )
            elif malformed_reason is not None:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason=malformed_reason
                )
            elif incomplete_reason is not None:
                result[rung.rung_index] = OperatorVoidEvidence(
                    "unknown", scope, reason=incomplete_reason
                )
            else:
                result[rung.rung_index] = OperatorVoidEvidence("absent", scope)
        return result
    except Exception as exc:
        reason = describe_exception(exc)
        if window_from is not None and window_to is not None:
            return {
                rung.rung_index: OperatorVoidEvidence(
                    "unknown",
                    _toss_lookup_scope(
                        window_from=window_from,
                        window_to=window_to,
                        rung_window=rung_windows[rung.rung_index],
                        closed_pages=closed_pages,
                        complete=False,
                        combination_matches=0,
                    ),
                    reason=reason,
                )
                for rung in rungs
            }
        scope = "toss GET /orders OPEN + CLOSED invalid temporal window"
        return {
            rung.rung_index: OperatorVoidEvidence("unknown", scope, reason=reason)
            for rung in rungs
        }
    finally:
        if owns_client and client is not None:
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
