"""Resting-class auto-approval eligibility policy (ROB-871, §40차).

This module is deliberately pure: it classifies one freshly previewed rung
and never reads the database or calls a broker. The dispatch/revalidation
boundary supplies the account's same-day cumulative auto-approved notional.

Two classifications live here, selected by ``AutoApproveLimits.mode``
(``settings.ORDER_PROPOSALS_AUTO_APPROVE_MODE``, default ``"off"``):

``off``
    ROB-871 as shipped: a rung must rest at least ``min_distance_pct`` away
    from the market on either side.

``expanded`` (§40차)
    Auto-approve, veto afterwards:
      1. buy rungs (their envelope / sizing / funding gates ran at create time), and
      2. profit-take sells whose expected realized P&L at the limit price is
         strictly positive *after* round-trip costs.
    Everything else goes to a human, fail-closed:
      * ``loss_cut`` exit intent (or any other exit intent),
      * expected realized P&L <= 0 -- exactly zero is not "> 0",
      * the ±``breakeven_band_pct`` break-even band around avg cost, whatever
        the sign of the P&L,
      * a ``policy_deviation`` / ``table_disagreement`` tag anywhere on the
        proposal,
      * anything that cannot be classified from the fresh preview.

    ``expanded`` drops ``min_distance_pct`` but NOT the requirement that the
    rung actually rest: a marketable order can fill before the operator sees
    the card, which would make the veto button (§40차 safety invariant ①) a
    lie. A buy must price strictly below the market and a sell strictly above
    it -- a limit exactly ON the market is marketable and is rejected. That is
    narrower than the §40차 literal, which is the permitted direction -- see
    docs/runbooks/order-proposal-auto-approve-expand.md §3.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.services.order_proposals.approval_message import (
    _escape_inline_code,
    _escape_markdown,
    build_callback_data,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
)
from app.services.trading_policy_service import load_trading_policy

_POLICY_MARKET = {
    "equity_kr": "kr",
    "equity_us": "us",
    "crypto": "crypto",
}

# Account/market combinations whose auto-veto Telegram button can actually
# cancel the just-submitted order (see telegram_callback._handle_auto_veto ->
# cancel_auto_submitted_rungs -> cancel_target_order). Deliberately NOT
# broker_gateway.SUPPORTED_TARGET_ACTIONS: that set also gates
# order_proposal_create's cancel/replace target-action support (ROB-972 added
# toss_live there for that purpose only). "Can a human-created replace/cancel
# proposal target this broker order" and "is this account mode eligible to
# skip the Telegram click entirely via auto-approve" are different questions
# -- widening one must never silently widen the other.
_VETO_CAPABLE_ACCOUNT_MARKETS = frozenset(
    {
        ("kis_live", "equity_kr"),
        ("kis_live", "equity_us"),
        ("upbit", "crypto"),
    }
)

# §40차: a proposal carrying either tag is a human's call regardless of how it
# prices. The tags have no column of their own, so the scan is deliberately
# over-inclusive -- it walks every free-text and JSON field a proposer can
# write to and matches the bare token anywhere inside. A false positive costs
# one Telegram tap; a false negative auto-submits an order the operator wanted
# to see.
_APPROVAL_REQUIRED_TAGS = frozenset({"policy_deviation", "table_disagreement"})
_TAG_SCAN_FIELDS = (
    "rationale",
    "source_asof",
    "lot_context",
    "thesis",
    "strategy",
    "exit_reason",
    "void_reason",
)

# Both-leg cost floor in basis points, per market. NOT measured rates: the two
# veto-capable ledgers (review.kis_live_order_ledger, review.live_order_ledger)
# carry no commission/tax columns, so this repo holds no realized fee evidence
# for kis_live or upbit to derive one from. Until it does, these are the
# conservative maximum of the rates the repo already declares -- see the
# provenance comment on `order_proposals.auto_approve.round_trip_cost_bps` in
# config/trading_policy.yaml. The policy value is raised to this floor, so an
# operator edit can only ever narrow the profit-take test, never widen it.
_ROUND_TRIP_COST_BPS_FLOOR = {
    "equity_kr": Decimal("47.4"),
    "equity_us": Decimal("90"),
    "crypto": Decimal("10"),
}


@dataclass(frozen=True)
class AutoApproveLimits:
    min_distance_pct: Decimal
    per_order_cap: Decimal
    daily_cap: Decimal
    policy_version: str
    # Defaults keep every existing construction site on ROB-871 behaviour.
    # `round_trip_cost_bps` defaults to the widest floor so a limits object
    # built without one still classifies conservatively.
    mode: str = "off"
    breakeven_band_pct: Decimal = Decimal("1")
    round_trip_cost_bps: Decimal = Decimal("90")


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
    declared_cost = Decimal(str(policy.round_trip_cost_bps[policy_market]))
    return AutoApproveLimits(
        min_distance_pct=Decimal(str(policy.min_distance_pct)),
        per_order_cap=Decimal(str(policy.per_order_cap[policy_market])),
        daily_cap=Decimal(str(policy.daily_cap[policy_market])),
        policy_version=document.version,
        mode=str(settings.ORDER_PROPOSALS_AUTO_APPROVE_MODE),
        breakeven_band_pct=Decimal(str(policy.breakeven_band_pct)),
        # The floor wins whenever the policy is cheaper than it.
        round_trip_cost_bps=max(
            declared_cost,
            _ROUND_TRIP_COST_BPS_FLOOR.get(
                market, max(_ROUND_TRIP_COST_BPS_FLOOR.values())
            ),
        ),
    )


def find_approval_required_tags(group: Any) -> tuple[str, ...]:
    """Return the §40차 approval-required tags carried anywhere on ``group``.

    Fails closed: if any field resists serialization the proposal is treated
    as carrying every tag, which routes it to a human.
    """
    parts: list[str] = []
    try:
        for field in _TAG_SCAN_FIELDS:
            value = getattr(group, field, None)
            if value is None:
                continue
            parts.append(
                value
                if isinstance(value, str)
                else json.dumps(value, default=str, ensure_ascii=False)
            )
    except Exception:  # noqa: BLE001 - unreadable metadata is not a clearance
        return tuple(sorted(_APPROVAL_REQUIRED_TAGS))
    haystack = "\n".join(parts).lower()
    return tuple(sorted(tag for tag in _APPROVAL_REQUIRED_TAGS if tag in haystack))


@dataclass(frozen=True)
class SellProfitVerdict:
    """Fee-netted profit-take classification for one sell rung (§40차 ②)."""

    verdict: str  # take_profit | breakeven_band | not_profitable | unclassifiable
    details: dict[str, str]


def classify_sell_profit(
    *,
    limit_price: Decimal,
    quantity: Decimal,
    preview: dict[str, Any],
    limits: AutoApproveLimits,
) -> SellProfitVerdict:
    """Decide whether a sell is a *proven* profit-take, failing closed.

    Order matters: the break-even band is checked before the P&L sign, because
    §40차 sends the band to a human "whatever the sign" -- a sell 0.5% above
    avg cost is inside the band even though its gross P&L is positive.
    """
    avg_buy_price = _decimal(preview.get("avg_buy_price"))
    if avg_buy_price is None or avg_buy_price <= 0:
        # No cost basis => no P&L => no classification. The avg*1.01 preview
        # guard fails open on unknown cost basis (order_validation), so a
        # passing preview is not evidence of profit here.
        return SellProfitVerdict("unclassifiable", {"avg_buy_price": "unavailable"})

    band = avg_buy_price * limits.breakeven_band_pct / Decimal("100")
    distance_from_avg = limit_price - avg_buy_price
    base = {
        "avg_buy_price": _text(avg_buy_price),
        "breakeven_band_pct": _text(limits.breakeven_band_pct),
        "round_trip_cost_bps": _text(limits.round_trip_cost_bps),
    }
    if abs(distance_from_avg) <= band:
        return SellProfitVerdict(
            "breakeven_band",
            {**base, "distance_from_avg": _text(distance_from_avg)},
        )

    # Charge the whole round trip against the larger of the two legs. Both legs
    # are charged at the same rate rather than split, which overstates the cost
    # -- the direction that narrows the profit-take test.
    cost = (
        max(limit_price, avg_buy_price)
        * quantity
        * limits.round_trip_cost_bps
        / Decimal("10000")
    )
    gross = distance_from_avg * quantity
    # The preview computes its own gross P&L from the same avg cost. If it is
    # more pessimistic than ours, believe it.
    preview_gross = _decimal(preview.get("realized_pnl"))
    if preview_gross is not None:
        gross = min(gross, preview_gross)
    net = gross - cost
    details = {
        **base,
        "gross_pnl": _text(gross),
        "round_trip_cost": _text(cost),
        "net_pnl": _text(net),
    }
    # Strictly greater than zero: exactly break-even is not a profit.
    return SellProfitVerdict("take_profit" if net > 0 else "not_profitable", details)


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

    mode = limits.mode
    if mode not in ("off", "expanded"):
        # An unrecognised mode is not a licence to submit.
        return reject("unknown_auto_approve_mode", mode=str(mode))
    base["mode"] = mode

    if (getattr(group, "action", None) or "place") != "place":
        return reject("action_not_place")
    if getattr(group, "order_type", None) != "limit":
        return reject("order_type_not_limit")
    exit_intent = getattr(group, "exit_intent", None)
    if exit_intent == "loss_cut":
        # §40차: a loss cut is always a human's call. Kept as its own reason so
        # the audit row says why, and so a future exit-intent vocabulary cannot
        # dilute this branch.
        return reject("loss_cut_intent")
    if exit_intent is not None:
        return reject("exit_intent_present", exit_intent=str(exit_intent))
    if (
        getattr(group, "account_mode", None),
        getattr(group, "market", None),
    ) not in _VETO_CAPABLE_ACCOUNT_MARKETS:
        return reject("account_not_veto_capable")
    # Applied in both modes: this can only ever reject.
    tags = find_approval_required_tags(group)
    if tags:
        return reject("approval_required_tag", tags=",".join(tags))
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

    side = getattr(rung, "side", None)
    if side not in ("buy", "sell"):
        return reject("side_not_supported")

    # `expanded` drops the min_distance_pct floor but still requires the rung
    # to rest: a buy at or above the market (a sell at or below it) can fill
    # before the operator ever sees the veto card. Hence the strict comparison
    # in `expanded` -- a limit exactly ON the market is marketable. `off` keeps
    # ROB-871's non-strict boundary (a rung exactly `min_distance_pct` away is
    # eligible), so this cannot change any verdict the shipped mode reaches.
    expanded = mode == "expanded"
    min_fraction = (
        Decimal("0") if expanded else limits.min_distance_pct / Decimal("100")
    )
    profit_details: dict[str, str] = {}
    if side == "buy":
        threshold = current_price * (Decimal("1") - min_fraction)
        distance_pct = (current_price - limit_price) / current_price * Decimal("100")
        if (limit_price >= threshold) if expanded else (limit_price > threshold):
            return reject(
                "marketable_not_resting" if expanded else "distance_below_minimum"
            )
        loss_guard = "not_applicable"
    else:
        threshold = current_price * (Decimal("1") + min_fraction)
        distance_pct = (limit_price - current_price) / current_price * Decimal("100")
        if (limit_price <= threshold) if expanded else (limit_price < threshold):
            return reject(
                "marketable_not_resting" if expanded else "distance_below_minimum"
            )
        # A successful fresh sell preview means the existing avg-cost loss
        # guard ran and passed. We record that provenance instead of
        # reimplementing the guard with a potentially different threshold.
        loss_guard = "preview_passed"
        if expanded:
            # ...but the preview guard fails open on unknown cost basis and is
            # bypassable (defensive_trim / loss_cut / mock), so `expanded`
            # proves the profit itself rather than inheriting that verdict.
            verdict = classify_sell_profit(
                limit_price=limit_price,
                quantity=quantity,
                preview=preview,
                limits=limits,
            )
            profit_details = verdict.details
            if verdict.verdict != "take_profit":
                return reject(
                    {
                        "breakeven_band": "breakeven_band",
                        "not_profitable": "expected_pnl_not_positive",
                        "unclassifiable": "sell_classification_unavailable",
                    }[verdict.verdict],
                    **verdict.details,
                )
            loss_guard = "net_profit_proven"

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
            **profit_details,
        },
    )


def build_auto_approved_message(
    *,
    group: Any,
    rungs: list[Any],
    nonce: str,
    policy_version: str,
    binding: DispatchBinding,
) -> tuple[str, dict[str, Any]]:
    """Render a compact post-submit summary with a single-use veto button."""
    if binding.card_kind is not ApprovalCardKind.AUTO_VETO:
        raise ValueError("auto-veto message requires an auto-veto binding")
    callback = build_callback_data(
        action="vc",
        proposal_id=group.proposal_id,
        nonce=nonce,
        binding=binding,
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
    "SellProfitVerdict",
    "build_auto_approved_message",
    "classify_sell_profit",
    "evaluate_auto_approve_eligibility",
    "find_approval_required_tags",
    "limits_for_market",
]
