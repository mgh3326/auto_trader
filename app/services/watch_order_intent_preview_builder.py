"""Pure preview builder for watch order intents (ROB-103).

Inputs are value objects + primitives. Outputs are an
``IntentBuildSuccess`` (full ROB-100 ``OrderPreviewLine``/``OrderBasketPreview``
plus KRW evaluation) or an ``IntentBuildFailure`` describing the reason.

No I/O. No DB, Redis, HTTP, settings, or logging side effects.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Final, Literal

from app.schemas.execution_contracts import (
    ExecutionGuard,
    ExecutionReadiness,
    OrderBasketPreview,
    OrderPreviewLine,
)
from app.services.watch_intent_policy import IntentPolicy

ACCOUNT_MODE: Final[Literal["kis_mock"]] = "kis_mock"
EXECUTION_SOURCE: Final[Literal["watch"]] = "watch"


@dataclass(frozen=True, slots=True)
class IntentBuildSuccess:
    preview_line: OrderPreviewLine
    basket: OrderBasketPreview
    notional_krw_evaluated: Decimal
    fx_usd_krw_used: Decimal | None


@dataclass(frozen=True, slots=True)
class IntentBuildFailure:
    blocked_by: str
    blocking_reasons: list[str]
    notional_krw_evaluated: Decimal | None
    fx_usd_krw_used: Decimal | None
    quantity: Decimal | None
    limit_price: Decimal | None
    currency: str | None


IntentBuildResult = IntentBuildSuccess | IntentBuildFailure


def _resolve_limit_price(policy: IntentPolicy, watch: dict) -> Decimal:
    if policy.limit_price is not None:
        return policy.limit_price
    return Decimal(str(watch["threshold"]))


def _resolve_quantity(policy: IntentPolicy, limit_price: Decimal) -> Decimal | None:
    if policy.quantity is not None:
        return Decimal(policy.quantity)
    assert policy.notional_krw is not None  # parser guarantees XOR
    raw = (policy.notional_krw / limit_price).to_integral_value(rounding=ROUND_FLOOR)
    if raw < 1:
        return None
    return raw


def _failure(
    *,
    blocked_by: str,
    quantity: Decimal | None,
    limit_price: Decimal | None,
    currency: str | None,
    notional_krw_evaluated: Decimal | None,
    fx_usd_krw_used: Decimal | None,
    extra_reasons: list[str] | None = None,
) -> IntentBuildFailure:
    reasons = [blocked_by]
    if extra_reasons:
        reasons.extend(extra_reasons)
    return IntentBuildFailure(
        blocked_by=blocked_by,
        blocking_reasons=reasons,
        notional_krw_evaluated=notional_krw_evaluated,
        fx_usd_krw_used=fx_usd_krw_used,
        quantity=quantity,
        limit_price=limit_price,
        currency=currency,
    )


def build_preview(
    *,
    policy: IntentPolicy,
    watch: dict,
    triggered_value: Decimal,  # noqa: ARG001 — recorded by the service, not used here yet
    fx_quote: Decimal | None,
    kst_date: str,  # noqa: ARG001 — service uses for idempotency_key
) -> IntentBuildResult:
    market = watch["market"]
    currency = "KRW" if market == "kr" else "USD"
    limit_price = _resolve_limit_price(policy, watch)

    quantity = _resolve_quantity(policy, limit_price)
    if quantity is None:
        return _failure(
            blocked_by="qty_zero",
            quantity=None,
            limit_price=limit_price,
            currency=currency,
            notional_krw_evaluated=None,
            fx_usd_krw_used=None,
        )

    native_notional = quantity * limit_price

    if market == "us":
        if fx_quote is None:
            return _failure(
                blocked_by="fx_unavailable",
                quantity=quantity,
                limit_price=limit_price,
                currency=currency,
                notional_krw_evaluated=None,
                fx_usd_krw_used=None,
            )
        notional_krw_evaluated = native_notional * fx_quote
        fx_used: Decimal | None = fx_quote
    else:
        notional_krw_evaluated = native_notional
        fx_used = None

    if (
        policy.max_notional_krw is not None
        and notional_krw_evaluated > policy.max_notional_krw
    ):
        return _failure(
            blocked_by="max_notional_krw_cap",
            quantity=quantity,
            limit_price=limit_price,
            currency=currency,
            notional_krw_evaluated=notional_krw_evaluated,
            fx_usd_krw_used=fx_used,
        )

    guard = ExecutionGuard(
        execution_allowed=False,
        approval_required=True,
        blocking_reasons=[],
        warnings=[],
    )
    line = OrderPreviewLine(
        symbol=watch["symbol"],
        market=market,
        side=policy.side,
        account_mode=ACCOUNT_MODE,
        execution_source=EXECUTION_SOURCE,
        lifecycle_state="previewed",
        quantity=quantity,
        limit_price=limit_price,
        notional=native_notional,
        currency=currency,
        guard=guard,
        rationale=[
            f"watch trigger {watch['condition_type']} threshold={watch['threshold']}",
            f"sizing_source={'notional_krw' if policy.quantity is None else 'quantity'}",
        ],
        correlation_id=None,
    )
    basket = OrderBasketPreview(
        account_mode=ACCOUNT_MODE,
        execution_source=EXECUTION_SOURCE,
        readiness=ExecutionReadiness(
            account_mode=ACCOUNT_MODE,
            execution_source=EXECUTION_SOURCE,
            is_ready=False,
            guard=guard,
        ),
        lines=[line],
        basket_warnings=[],
    )
    return IntentBuildSuccess(
        preview_line=line,
        basket=basket,
        notional_krw_evaluated=notional_krw_evaluated,
        fx_usd_krw_used=fx_used,
    )


__all__ = [
    "ACCOUNT_MODE",
    "EXECUTION_SOURCE",
    "IntentBuildFailure",
    "IntentBuildResult",
    "IntentBuildSuccess",
    "build_preview",
]
