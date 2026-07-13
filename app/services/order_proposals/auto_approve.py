"""Resting-class auto-approval eligibility policy (ROB-871).

This module is deliberately pure: it classifies one freshly previewed rung
and never reads the database or calls a broker. The dispatch/revalidation
boundary supplies the account's same-day cumulative auto-approved notional.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.order_proposals.approval_message import (
    _escape_inline_code,
    _escape_markdown,
    build_callback_data,
)
from app.services.order_proposals.broker_gateway import SUPPORTED_TARGET_ACTIONS
from app.services.trading_policy_service import load_trading_policy

_POLICY_MARKET = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}


@dataclass(frozen=True)
class AutoApproveLimits:
    min_distance_pct: Decimal
    per_order_cap: Decimal
    daily_cap: Decimal
    policy_version: str


@dataclass(frozen=True)
class AutoApproveDecision:
    eligible: bool
    reason: str
    details: dict[str, str]


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def limits_for_market(market: str) -> AutoApproveLimits | None:
    policy_market = _POLICY_MARKET.get(market)
    if policy_market is None:
        return None
    document = load_trading_policy()
    policy = document.order_proposals.auto_approve
    return AutoApproveLimits(
        min_distance_pct=Decimal(str(policy.min_distance_pct)),
        per_order_cap=Decimal(str(policy.per_order_cap[policy_market])),
        daily_cap=Decimal(str(policy.daily_cap[policy_market])),
        policy_version=document.version,
    )


def evaluate_auto_approve_eligibility(
    *,
    group: Any,
    rung: Any,
    preview: dict[str, Any],
    limits: AutoApproveLimits,
    daily_notional: Decimal,
) -> AutoApproveDecision:
    """Classify a rung using the fresh submit-time preview, failing closed."""

    base = {"policy_version": limits.policy_version}

    def reject(reason: str, **details: str) -> AutoApproveDecision:
        return AutoApproveDecision(False, reason, {**base, **details})

    if (getattr(group, "action", None) or "place") != "place":
        return reject("action_not_place")
    if getattr(group, "order_type", None) != "limit":
        return reject("order_type_not_limit")
    if getattr(group, "exit_intent", None) is not None:
        return reject("exit_intent_present")
    if (
        getattr(group, "account_mode", None),
        getattr(group, "market", None),
    ) not in SUPPORTED_TARGET_ACTIONS:
        return reject("account_not_veto_capable")
    if preview.get("success") is not True:
        return reject("preview_guard_failed")

    current_price = _decimal(preview.get("current_price"))
    limit_price = _decimal(getattr(rung, "limit_price", None))
    quantity = _decimal(getattr(rung, "quantity", None))
    if (
        current_price is None
        or current_price <= 0
        or limit_price is None
        or limit_price <= 0
        or quantity is None
        or quantity <= 0
    ):
        return reject("price_or_quantity_missing")

    # Use the executable price × quantity, never proposer-supplied advisory
    # notional, so a stale or understated metadata field cannot bypass caps.
    notional = limit_price * quantity
    if notional > limits.per_order_cap:
        return reject(
            "per_order_cap_exceeded",
            notional=_text(notional),
            per_order_cap=_text(limits.per_order_cap),
        )
    daily_after = daily_notional + notional
    if daily_after > limits.daily_cap:
        return reject(
            "daily_cap_exceeded",
            daily_notional_after=_text(daily_after),
            daily_cap=_text(limits.daily_cap),
        )

    min_fraction = limits.min_distance_pct / Decimal("100")
    side = getattr(rung, "side", None)
    if side == "buy":
        threshold = current_price * (Decimal("1") - min_fraction)
        distance_pct = (current_price - limit_price) / current_price * Decimal("100")
        if limit_price > threshold:
            return reject("distance_below_minimum")
        loss_guard = "not_applicable"
    elif side == "sell":
        threshold = current_price * (Decimal("1") + min_fraction)
        distance_pct = (limit_price - current_price) / current_price * Decimal("100")
        if limit_price < threshold:
            return reject("distance_below_minimum")
        # A successful fresh sell preview means the existing avg-cost loss
        # guard ran and passed. We record that provenance instead of
        # reimplementing the guard with a potentially different threshold.
        loss_guard = "preview_passed"
    else:
        return reject("side_not_supported")

    return AutoApproveDecision(
        True,
        "eligible",
        {
            **base,
            "current_price": _text(current_price),
            "limit_price": _text(limit_price),
            "distance_pct": _text(distance_pct),
            "min_distance_pct": _text(limits.min_distance_pct),
            "notional": _text(notional),
            "daily_notional_before": _text(daily_notional),
            "daily_notional_after": _text(daily_after),
            "per_order_cap": _text(limits.per_order_cap),
            "daily_cap": _text(limits.daily_cap),
            "loss_guard": loss_guard,
        },
    )


def build_auto_approved_message(
    *, group: Any, rungs: list[Any], nonce: str, policy_version: str
) -> tuple[str, dict[str, Any]]:
    """Render a compact post-submit summary with a single-use veto button."""
    callback = build_callback_data(
        action="vc", proposal_id=group.proposal_id, nonce=nonce
    )
    lines = [
        "✅ *자동 접수됨*",
        f"- 종목: `{_escape_inline_code(group.symbol)}`",
        f"- 방향: `{_escape_inline_code(group.side)}`",
    ]
    for rung in sorted(rungs, key=lambda item: item.rung_index):
        lines.append(f"- #{rung.rung_index + 1}: {rung.quantity} × {rung.limit_price}")
    rationale = " ".join(str(group.thesis or group.strategy or "근거 미기재").split())
    if len(rationale) > 120:
        rationale = rationale[:119] + "…"
    lines.extend(
        [
            f"- 근거: {_escape_markdown(rationale)}",
            f"- `auto:policy@{policy_version}`",
        ]
    )
    return "\n".join(lines), {
        "inline_keyboard": [[{"text": "취소", "callback_data": callback}]]
    }


__all__ = [
    "AutoApproveDecision",
    "AutoApproveLimits",
    "evaluate_auto_approve_eligibility",
    "build_auto_approved_message",
    "limits_for_market",
]
