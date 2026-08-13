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
        # TOSS-AUTO-FULL: this membership is *not* sufficient by itself.
        # ``_is_veto_capable_account_market`` keeps both Toss surfaces
        # default-disabled behind the independently armed setting below.
        ("toss_live", "equity_kr"),
        ("toss_live", "equity_us"),
    }
)

_TOSS_LIVE_VETO_ACCOUNT_MARKETS = frozenset(
    {
        ("toss_live", "equity_kr"),
        ("toss_live", "equity_us"),
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
_TAG_PATH_KEY_ALLOWLIST = frozenset(
    {
        "context",
        "decision",
        "flags",
        "labels",
        "metadata",
        "notes",
        "reason",
        "review",
        "tag",
        "tags",
    }
)
_MAX_TAG_MATCHES = 24

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
    details: dict[str, Any]


def _decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _text(value: Decimal) -> str:
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".") if "." in normalized else normalized


def _known_value(value: Any, allowed: frozenset[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unrecognized"


def auto_veto_thesis_summary(group: Any) -> str | None:
    """Return the mandatory, bounded thesis for an auto-veto card.

    A strategy label is not a thesis: accepting it as a fallback would turn a
    missing reason into a seemingly complete cancellation card.  The caller
    must route the proposal to ordinary human approval whenever this returns
    ``None``.
    """
    thesis = getattr(group, "thesis", None)
    if not isinstance(thesis, str):
        return None
    normalized = " ".join(thesis.split())
    if not normalized:
        return None
    return normalized[:119] + "…" if len(normalized) > 120 else normalized


def _is_veto_capable_account_market(account_mode: Any, market: Any) -> bool:
    candidate = (account_mode, market)
    if candidate not in _VETO_CAPABLE_ACCOUNT_MARKETS:
        return False
    if candidate in _TOSS_LIVE_VETO_ACCOUNT_MARKETS:
        # The live Toss expansion is explicitly a second gate.  The default is
        # false, including when the master auto-approve gate is switched on.
        return bool(settings.ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED)
    return True


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


@dataclass(frozen=True)
class _ApprovalRequiredTagScan:
    fields: tuple[tuple[str, Any, str], ...]
    tags: tuple[str, ...]
    failed: bool


def _scan_approval_required_tags(group: Any) -> _ApprovalRequiredTagScan:
    """Scan once so an audit match cannot alter the classification result."""
    fields: list[tuple[str, Any, str]] = []
    try:
        for field in _TAG_SCAN_FIELDS:
            value = getattr(group, field, None)
            if value is None:
                continue
            fields.append(
                (
                    field,
                    value,
                    value
                    if isinstance(value, str)
                    else json.dumps(value, default=str, ensure_ascii=False),
                )
            )
    except Exception:  # noqa: BLE001 - unreadable metadata is not a clearance
        return _ApprovalRequiredTagScan(
            (), tuple(sorted(_APPROVAL_REQUIRED_TAGS)), True
        )
    haystack = "\n".join(rendered for _, _, rendered in fields).lower()
    return _ApprovalRequiredTagScan(
        tuple(fields),
        tuple(sorted(tag for tag in _APPROVAL_REQUIRED_TAGS if tag in haystack)),
        False,
    )


def find_approval_required_tags(group: Any) -> tuple[str, ...]:
    """Return the §40차 approval-required tags carried anywhere on ``group``.

    Fails closed: if any field resists serialization the proposal is treated
    as carrying every tag, which routes it to a human.
    """
    return _scan_approval_required_tags(group).tags


def _path_for_json_child(path: str, key: Any, index: int) -> str:
    """Keep evidence locatable without exposing arbitrary JSON keys."""
    if isinstance(key, str) and key in _TAG_PATH_KEY_ALLOWLIST:
        return f"{path}.{key}"
    return f"{path}[{index}]"


def _find_text_matches(
    text: str,
    *,
    token: str,
    field: str,
    path: str,
    kind: str,
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    haystack = text.lower()
    start = 0
    while len(matches) < _MAX_TAG_MATCHES:
        found = haystack.find(token, start)
        if found < 0:
            break
        matches.append(
            {
                "token": token,
                "field": field,
                "path": path,
                "kind": kind,
                "char_start": found,
            }
        )
        start = found + len(token)
    return matches


def _find_tag_matches_in_value(
    value: Any,
    *,
    token: str,
    field: str,
    path: str,
    nested: bool,
) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return _find_text_matches(
            value,
            token=token,
            field=field,
            path=path,
            kind="json_value" if nested else "text",
        )
    if isinstance(value, dict):
        matches: list[dict[str, Any]] = []
        for index, (key, child) in enumerate(value.items()):
            child_path = _path_for_json_child(path, key, index)
            if isinstance(key, str):
                matches.extend(
                    _find_text_matches(
                        key,
                        token=token,
                        field=field,
                        path=child_path,
                        kind="json_key",
                    )
                )
            matches.extend(
                _find_tag_matches_in_value(
                    child,
                    token=token,
                    field=field,
                    path=child_path,
                    nested=True,
                )
            )
            if len(matches) >= _MAX_TAG_MATCHES:
                return matches[:_MAX_TAG_MATCHES]
        return matches
    if isinstance(value, (list, tuple)):
        matches = []
        for index, child in enumerate(value):
            matches.extend(
                _find_tag_matches_in_value(
                    child,
                    token=token,
                    field=field,
                    path=f"{path}[{index}]",
                    nested=True,
                )
            )
            if len(matches) >= _MAX_TAG_MATCHES:
                return matches[:_MAX_TAG_MATCHES]
        return matches
    return []


def _scan_unavailable_matches(tags: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {
            "token": token,
            "field": "source_asof",
            "path": "$",
            "kind": "scan_unavailable",
            "char_start": 0,
        }
        for token in tags
    ]


def _tag_matches_from_scan(scan: _ApprovalRequiredTagScan) -> list[dict[str, Any]]:
    if not scan.tags:
        return []
    if scan.failed:
        return _scan_unavailable_matches(scan.tags)

    try:
        matches: list[dict[str, Any]] = []
        for token in scan.tags:
            for field, value, rendered in scan.fields:
                locations = _find_tag_matches_in_value(
                    value, token=token, field=field, path="$", nested=False
                )
                if not locations and token in rendered.lower():
                    locations = _find_text_matches(
                        rendered,
                        token=token,
                        field=field,
                        path="$",
                        kind="serialized",
                    )
                matches.extend(locations)
                if len(matches) >= _MAX_TAG_MATCHES:
                    return matches[:_MAX_TAG_MATCHES]
        return matches
    except Exception:  # noqa: BLE001 - audit evidence must not clear a veto
        return _scan_unavailable_matches(scan.tags)


def find_approval_required_tag_matches(group: Any) -> list[dict[str, Any]]:
    """Return only token + structural location, never the matched free text."""
    return _tag_matches_from_scan(_scan_approval_required_tags(group))


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

    def reject(reason: str, **details: Any) -> AutoApproveDecision:
        return AutoApproveDecision(False, reason, {**base, **details})

    mode = limits.mode
    if mode not in ("off", "expanded"):
        # An unrecognised mode is not a licence to submit.
        return reject("unknown_auto_approve_mode", mode="unrecognized")
    base["mode"] = mode

    action = getattr(group, "action", None) or "place"
    if action != "place":
        return reject(
            "action_not_place",
            action=_known_value(action, frozenset({"place", "replace", "cancel"})),
        )
    order_type = getattr(group, "order_type", None)
    if order_type != "limit":
        return reject(
            "order_type_not_limit",
            order_type=_known_value(order_type, frozenset({"limit", "market"})),
        )
    exit_intent = getattr(group, "exit_intent", None)
    if exit_intent == "loss_cut":
        # §40차: a loss cut is always a human's call. Kept as its own reason so
        # the audit row says why, and so a future exit-intent vocabulary cannot
        # dilute this branch.
        return reject("loss_cut_intent", exit_intent_present=True)
    if exit_intent is not None:
        return reject("exit_intent_present", exit_intent_present=True)
    account_mode = getattr(group, "account_mode", None)
    market = getattr(group, "market", None)
    if not _is_veto_capable_account_market(
        account_mode,
        market,
    ):
        return reject(
            "account_not_veto_capable",
            account_mode=_known_value(
                account_mode,
                frozenset(
                    {"kis_live", "kis_mock", "toss_live", "upbit", "db_simulated"}
                ),
            ),
            market=_known_value(
                market,
                frozenset({"equity_kr", "equity_us", "crypto", "forex", "index"}),
            ),
        )
    # Applied in both modes: this can only ever reject.
    tag_scan = _scan_approval_required_tags(group)
    tags = tag_scan.tags
    if tags:
        return reject(
            "approval_required_tag",
            tags=",".join(tags),
            tag_matches=_tag_matches_from_scan(tag_scan),
        )
    if preview.get("success") is not True:
        return reject(
            "preview_guard_failed",
            preview_success="false" if preview.get("success") is False else "invalid",
        )

    current_price = _decimal(preview.get("current_price"))
    limit_price = _decimal(getattr(rung, "limit_price", None))
    quantity = _decimal(getattr(rung, "quantity", None))
    missing_inputs = [
        name
        for name, value in (
            ("current_price", current_price),
            ("limit_price", limit_price),
            ("quantity", quantity),
        )
        if value is None or value <= 0
    ]
    if missing_inputs:
        return reject("price_or_quantity_missing", missing_inputs=missing_inputs)

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
        return reject(
            "side_not_supported",
            side=_known_value(side, frozenset({"buy", "sell"})),
        )

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
                "marketable_not_resting" if expanded else "distance_below_minimum",
                current_price=_text(current_price),
                limit_price=_text(limit_price),
                quantity=_text(quantity),
                threshold=_text(threshold),
                distance_pct=_text(distance_pct),
                min_distance_pct=_text(limits.min_distance_pct),
            )
        loss_guard = "not_applicable"
    else:
        threshold = current_price * (Decimal("1") + min_fraction)
        distance_pct = (limit_price - current_price) / current_price * Decimal("100")
        if (limit_price <= threshold) if expanded else (limit_price < threshold):
            return reject(
                "marketable_not_resting" if expanded else "distance_below_minimum",
                current_price=_text(current_price),
                limit_price=_text(limit_price),
                quantity=_text(quantity),
                threshold=_text(threshold),
                distance_pct=_text(distance_pct),
                min_distance_pct=_text(limits.min_distance_pct),
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

    if auto_veto_thesis_summary(group) is None:
        # The post-submit card must name the decision reason.  This sits after
        # every existing safety classifier so missing prose never masks a
        # stronger reason (loss-cut, cap, marketability, tag, or unknown
        # classification), while an otherwise eligible order still cannot
        # reach the broker with an unrenderable veto card.
        return reject("thesis_required_for_veto_card", thesis_present=False)

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
    ordered_rungs = sorted(rungs, key=lambda item: item.rung_index)
    quantities = ", ".join(
        f"#{rung.rung_index + 1} {rung.quantity}" for rung in ordered_rungs
    )
    prices = ", ".join(
        f"#{rung.rung_index + 1} {rung.limit_price}" for rung in ordered_rungs
    )
    rationale = auto_veto_thesis_summary(group)
    if rationale is None:
        raise ValueError("auto-veto card requires a non-empty thesis")
    lines.extend(
        [
            f"- 수량: {quantities}",
            f"- 가격: {prices}",
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
    "auto_veto_thesis_summary",
    "build_auto_approved_message",
    "classify_sell_profit",
    "evaluate_auto_approve_eligibility",
    "find_approval_required_tags",
    "find_approval_required_tag_matches",
    "limits_for_market",
]
