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
      * a ``policy_deviation`` tag anywhere on the proposal,
      * anything that cannot be classified from the fresh preview.

    ``expanded`` drops ``min_distance_pct`` but normally still requires the
    rung to rest: a marketable order can fill before the operator sees the
    card, which would make the veto button (§40차 safety invariant ①) a lie.
    §156차's operator-approved exception is deliberately narrower than a
    general marketable-order release: only a *limit sell* whose fresh broker
    preview proves ``classify_sell_profit(...)=take_profit`` may be
    marketable. A buy must still price strictly below the market, and every
    loss, break-even-band, or unclassifiable sell still goes to a human. This
    makes the veto post-hoc for that proven profit-take sell only; see
    docs/runbooks/order-proposal-auto-approve-expand.md §3.

    The per-order and daily caps remain hard gates.  For that one marketable
    profit-take sell, both caps use ``max(limit_price, current_price) ×
    quantity`` so an executable price above the limit cannot understate the
    amount that automation is allowed to submit.  Every other rung keeps the
    established ``limit_price × quantity`` cap basis.

§141차 -- ``replace`` / ``cancel``
    Until §141차 this classifier rejected every non-``place`` action outright
    (``action_not_place``), so a cancel or a replace always cost the operator a
    Telegram tap no matter how ordinary it was. That exclusion is gone; the
    gates it stood in front of are not:

    * ``replace`` is classified as what it actually is -- a brand-new limit
      rung -- and runs the *entire* ``place`` stack: order type, exit intent,
      veto-capable account/market, approval-required tags, fresh preview,
      per-order and daily caps, marketability, and (for sells) the break-even
      band + round-trip-cost profit proof. §156's sole marketable exception is
      the same proven-profit limit sell; nothing else is skipped or loosened
      because the rung happens to replace an existing order.
    * ``cancel`` places no order, so the amount/marketability gates have no
      subject. It is cleared on target-ownership evidence plus the same
      loss-cut, account, tag and thesis gates, and consumes zero daily budget.

    Both additionally require the cancel/replace target evidence to be present
    and self-consistent (``target_evidence_missing``). An action outside
    ``{place, replace, cancel}`` still fails closed (``action_not_supported``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.config import settings
from app.core.portfolio_links import build_position_detail_url
from app.services.order_proposals.approval_message import (
    _build_order_core_metrics,
    _escape_inline_code,
    _escape_markdown,
    _format_datetime,
    _format_symbol_label,
    build_callback_data,
)
from app.services.order_proposals.auto_approve_audit import (
    AUTO_APPROVE_REJECTIONS_KEY,
)
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
)
from app.services.trading_policy_service import load_trading_policy, policy_content_hash

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

# §141차: cancel/replace are auto-approve candidates, not a categorically
# excluded class. `place` and `replace` share the whole amount/marketability
# gate stack (a replace *is* a new limit rung, with only §156's proven-profit
# marketable-sell exception); `cancel` reduces exposure and is gated on
# ownership evidence + the tag scan instead. Anything outside this vocabulary
# is rejected -- widening it is a deliberate act, never a default.
# Doubles as the audit vocabulary: an action not in here renders as
# "unrecognized" rather than leaking a proposer-supplied string into the ledger.
_SUPPORTED_ACTIONS = frozenset({"place", "replace", "cancel"})

# §156차: only ``policy_deviation`` is an approval blocker. The scanner stays
# deliberately over-inclusive -- it walks every free-text and JSON field a
# proposer can write to and matches the bare token anywhere inside, including
# JSON keys. That exact behavior continues to protect ``policy_deviation``;
# changing it to value-only matching would be a further, unauthorized
# relaxation. ``table_disagreement`` remains in the separate audit retention
# vocabulary so existing and future session records keep their evidence, but
# it is not an eligibility veto.
_APPROVAL_REQUIRED_TAGS = frozenset({"policy_deviation"})
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
    # Audit-only policy provenance. Eligibility deliberately never reads this value.
    # Kept last so legacy positional constructions retain their semantics.
    policy_content_hash: str | None = None


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
    content_hash = policy_content_hash()
    policy = document.order_proposals.auto_approve
    declared_cost = Decimal(str(policy.round_trip_cost_bps[policy_market]))
    return AutoApproveLimits(
        min_distance_pct=Decimal(str(policy.min_distance_pct)),
        per_order_cap=Decimal(str(policy.per_order_cap[policy_market])),
        daily_cap=Decimal(str(policy.daily_cap[policy_market])),
        policy_version=document.version,
        policy_content_hash=content_hash,
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
            if field == "source_asof" and isinstance(value, Mapping):
                # This is system-generated audit output, not proposal input.
                # Re-scanning it on a later dispatch would turn its own tag
                # evidence into a new match location and eventually crowd out
                # the original evidence under the match cap.
                value = {
                    key: child
                    for key, child in value.items()
                    if key != AUTO_APPROVE_REJECTIONS_KEY
                }
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


def _classify_target_evidence(group: Any) -> str | None:
    """Return why a cancel/replace target is unusable, or ``None`` if it is.

    Pure and deliberately narrow. The authoritative "is this order mine"
    answer comes from ``revalidation._validate_target_action``, which reads the
    order back from this account's own broker history and diffs it against the
    approved snapshot -- both of those run before this classifier is consulted.
    What this adds is that the classifier itself refuses to clear a target
    action whose evidence is absent or internally inconsistent, so a future
    caller that forgets the broker-side check cannot auto-approve a cancel or
    replace pointed at an order this proposal never proved it owns.
    """
    target_id = getattr(group, "target_broker_order_id", None)
    if not isinstance(target_id, str) or not target_id.strip():
        return "order_id_missing"
    source_asof = getattr(group, "source_asof", None)
    if not isinstance(source_asof, Mapping):
        return "snapshot_missing"
    snapshot = source_asof.get("target_order_snapshot")
    if not isinstance(snapshot, Mapping) or not snapshot:
        return "snapshot_missing"
    snapshot_id = snapshot.get("broker_order_id")
    if not isinstance(snapshot_id, str) or snapshot_id.strip() != target_id.strip():
        return "snapshot_mismatch"
    return None


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
    if action not in _SUPPORTED_ACTIONS:
        # §141차 removed the categorical `action_not_place` rejection, NOT the
        # fail-closed default: an action this classifier has never been taught
        # to gate is still a human's call.
        return reject(
            "action_not_supported",
            action=_known_value(action, _SUPPORTED_ACTIONS),
        )
    base["action"] = action
    if action in ("replace", "cancel"):
        # Ownership is proven at the broker by `_validate_target_action`, which
        # fetches this order id from *this account's* order history and compares
        # it to the approved snapshot before the gate runs. That check cannot
        # live here (this module is pure), so the pure invariant asserted here
        # is that the evidence the broker check consumes actually exists and is
        # self-consistent -- a proposal that reached this classifier without it
        # must never be auto-approved.
        target_outcome = _classify_target_evidence(group)
        if target_outcome is not None:
            return reject("target_evidence_missing", target_evidence=target_outcome)
    if action != "cancel":
        # A cancel places no order, so it has no order type to constrain.
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
    if action == "cancel":
        # §141차 ③: a cancel only ever *reduces* exposure, so the amount gates
        # (per-order cap, daily cap, min-distance, marketability, break-even
        # band, round-trip cost) have nothing to price. Everything that can
        # still reject a cancel has already run above: mode, supported action,
        # target evidence, loss-cut/exit intent, veto-capable account/market,
        # and the approval-required tag scan. The remaining requirement is the
        # card's own renderability.
        if auto_veto_thesis_summary(group) is None:
            return reject("thesis_required_for_veto_card", thesis_present=False)
        return AutoApproveDecision(
            True,
            "eligible",
            {
                **base,
                # A cancel consumes no daily budget. Reporting the unchanged
                # running total (rather than omitting the key) keeps the
                # dispatch accumulator and the cap-observation projection on
                # their existing contract while recording a truthful zero.
                "notional": "0",
                "daily_notional_before": _text(daily_notional),
                "daily_notional_after": _text(daily_notional),
                "per_order_cap": _text(limits.per_order_cap),
                "daily_cap": _text(limits.daily_cap),
                "loss_guard": "not_applicable",
            },
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

    # Start with the established booked limit price × quantity, never
    # proposer-supplied advisory notional, so a stale or understated metadata
    # field cannot bypass caps.  §156's one marketable profit-sell exception
    # receives a stricter execution-price adjustment only after its objective
    # profit proof below; keeping this preliminary check preserves the exact
    # existing cap behavior for every other rung.
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

    # `expanded` drops the min_distance_pct floor. A buy at or above the market
    # can fill before the operator sees the veto card, so it remains rejected.
    # A sell at or below it is equally marketable, but §156차 permits that one
    # risk only after the fresh broker preview proves a fee-netted take-profit.
    # That sell can fill before the veto card is visible; do not generalize this
    # exception to buys or to any other sell classification. `off` keeps
    # ROB-871's non-strict distance boundary, so it cannot inherit this release.
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
        marketable_profit_sell = expanded and limit_price <= threshold
        if not expanded and limit_price < threshold:
            return reject(
                "distance_below_minimum",
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
            if marketable_profit_sell:
                # Observable in the durable eligibility projection: this is the
                # sole §156 path on which a fill may precede the veto card.
                profit_details["marketability"] = "marketable_profit_take"
                # A marketable limit sell can execute at the current price, not
                # necessarily its (possibly deeply discounted) limit.  The
                # §106 per-order loss boundary and daily circuit breaker must
                # meter that executable amount.  The preliminary limit-based
                # check above intentionally remains in place for all rungs;
                # this is an additional, stricter check only after the narrow
                # §156 profit-take predicate has proved true.
                notional = max(limit_price, current_price) * quantity
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
    nonce: str | None,
    policy_version: str,
    display_name: str | None = None,
    binding: DispatchBinding | None,
) -> tuple[str, dict[str, Any]]:
    """Render a compact post-submit summary with a single-use veto button.

    §141차: an auto-approved ``cancel`` gets the same card *without* the veto
    button. A veto cancels the order this proposal just put on the book -- for
    a cancel proposal there is no such order, and the target it retired cannot
    be un-cancelled. Rendering the button anyway would offer an undo that does
    nothing while reporting "🛑 취소됨", which reads as a successful undo. The
    ``replace`` card keeps the button: its replacement rung is live and is
    exactly what a veto is for.
    """
    action = getattr(group, "action", None) or "place"
    vetoable = action != "cancel"
    if vetoable:
        if binding is None or binding.card_kind is not ApprovalCardKind.AUTO_VETO:
            raise ValueError("auto-veto message requires an auto-veto binding")
        if not nonce:
            # A vetoable card without a nonce would render a button no callback
            # can authorize. Fail loudly rather than publish a dead undo.
            raise ValueError("auto-veto card requires a nonce")
    elif binding is not None and binding.card_kind is not ApprovalCardKind.AUTO_VETO:
        raise ValueError("auto-veto message requires an auto-veto binding")
    callback = (
        build_callback_data(
            action="vc",
            proposal_id=group.proposal_id,
            nonce=nonce,
            binding=binding,
        )
        if vetoable
        else None
    )
    header = {
        "cancel": "✅ *자동 취소됨*",
        "replace": "✅ *자동 정정 접수됨*",
    }.get(action, "✅ *자동 접수됨*")
    lines = [
        header,
        f"- 종목: {_format_symbol_label(group.symbol, display_name=display_name)}",
        f"- 방향: `{_escape_inline_code(group.side)}`",
    ]
    target_id = getattr(group, "target_broker_order_id", None)
    if action in ("cancel", "replace") and isinstance(target_id, str) and target_id:
        lines.append(f"- 대상 주문: `{_escape_inline_code(target_id)}`")
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
            f"- 핵심 수치: {_build_order_core_metrics(group=group, rungs=ordered_rungs)}",
            f"- 근거: {_escape_markdown(rationale)}",
            f"- 유효기간: {_format_datetime(getattr(group, 'valid_until', None), approximate=False)}",
            f"- `auto:policy@{policy_version}`",
        ]
    )
    detail_url = build_position_detail_url(group.symbol, group.market)
    if detail_url is not None:
        lines.append(f"- /invest 상세: {detail_url}")
    rows: list[list[dict[str, Any]]] = []
    if callback is not None:
        rows.append([{"text": "취소", "callback_data": callback}])
    if detail_url is not None:
        rows.append([{"text": "🔎 /invest 상세", "url": detail_url}])
    return "\n".join(lines), {"inline_keyboard": rows}


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
