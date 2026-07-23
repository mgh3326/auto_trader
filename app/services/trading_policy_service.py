"""Loader for config/trading_policy.yaml — the single authoritative source
of trading judgment thresholds (ROB-646). Read-only; operator edits via PR."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

from app.schemas.trading_policy import (
    SingleShareExitDecisionRule,
    TradingPolicyDocument,
)

_POLICY_PATH: Path = (
    Path(__file__).resolve().parents[2] / "config" / "trading_policy.yaml"
)

_cache: dict[str, Any] = {"key": None, "doc": None, "hash": None}


class TradingPolicyKeyError(ValueError):
    """Unknown market or lane requested from the trading policy."""


@dataclass(frozen=True, slots=True)
class SingleShareExitEvaluation:
    """Pure policy result; it neither persists nor dispatches a proposal."""

    outcome: Literal["PROPOSE", "DEFER", "INELIGIBLE"]
    reason: str
    action: Literal["propose_full_exit"] | None = None
    sizing: Literal["full_position"] | None = None
    approval: Literal["telegram_manual"] | None = None
    auto_approve: bool = False
    execution: Literal["proposal_only"] | None = None


def _reset_cache_for_tests() -> None:
    _cache["key"] = None
    _cache["doc"] = None
    _cache["hash"] = None


def _load() -> tuple[TradingPolicyDocument, str]:
    stat = _POLICY_PATH.stat()
    key = (str(_POLICY_PATH), stat.st_mtime_ns, stat.st_size)
    if _cache["key"] == key and _cache["doc"] is not None:
        return _cache["doc"], _cache["hash"]
    raw_bytes = _POLICY_PATH.read_bytes()
    doc = TradingPolicyDocument.model_validate(yaml.safe_load(raw_bytes))
    content_hash = hashlib.sha256(raw_bytes).hexdigest()[:12]
    _cache.update(key=key, doc=doc, hash=content_hash)
    return doc, content_hash


def load_trading_policy() -> TradingPolicyDocument:
    return _load()[0]


def policy_content_hash() -> str:
    return _load()[1]


def policy_version_stamp() -> dict[str, str]:
    doc, content_hash = _load()
    return {"version": doc.version, "content_hash": content_hash}


def get_policy_for(market: str, lane: str) -> dict[str, Any]:
    doc, content_hash = _load()
    if market not in doc.market_overrides:
        raise TradingPolicyKeyError(
            f"unknown market {market!r}; valid: {sorted(doc.market_overrides)}"
        )
    valid_lanes = {"buy", "sell", "discovery"}
    if lane not in valid_lanes:
        raise TradingPolicyKeyError(
            f"unknown lane {lane!r}; valid: {sorted(valid_lanes)}"
        )
    overrides = doc.market_overrides[market]
    thresholds: dict[str, Any] = {}
    for key, spec in doc.thresholds.items():
        if lane not in spec.lanes:
            continue
        if key in overrides:
            value = overrides[key]
            source = "override"
        else:
            value = spec.value
            source = "default"
        thresholds[key] = {
            "value": value,
            "unit": spec.unit,
            "semantics": spec.semantics,
            "of": spec.of,
            "one_share_exception": (
                spec.one_share_exception.model_dump()
                if spec.one_share_exception is not None
                else None
            ),
            "source": source,
        }
    decision_rules = {
        key: spec.model_dump(exclude={"lanes"})
        for key, spec in doc.decision_rules.items()
        if lane in spec.lanes
    }
    market_rules: dict[str, Any] = {}
    rules = doc.market_rules.get("crypto") if market == "crypto" else None
    if rules is not None:
        for key in type(rules).model_fields:
            spec = getattr(rules, key)
            if lane in spec.lanes:
                market_rules[key] = spec.model_dump(exclude={"lanes"})
    return {
        "market": market,
        "lane": lane,
        "version": doc.version,
        "content_hash": content_hash,
        "thresholds": thresholds,
        "decision_rules": decision_rules,
        "market_rules": market_rules,
        # ROB-932 — single global advisory trigger, not market/lane-scoped;
        # echoed unconditionally alongside the version/content_hash stamp.
        "crash_day": doc.crash_day.model_dump(),
        # ROB-948 — global advisory stance context, not market/lane-scoped;
        # same echo pattern as crash_day above.
        "user_stances": [stance.model_dump() for stance in doc.user_stances],
    }


def sector_cluster_for(label: str | None) -> str | None:
    if not label:
        return None
    doc, _ = _load()
    needle = label.strip().casefold()
    for cluster, members in doc.sector_clusters.items():
        for member in members:
            m = member.strip().casefold()
            # ROB-646 Finding 3: one-directional (member is a substring of the
            # label). The reverse direction (label ⊂ member) widened the surface
            # and misclassified short labels; dropping it removes that class of
            # false positive while preserving KR prefix coverage.
            if m and m in needle:
                return cluster
    return None


def evaluate_single_share_exit(
    *,
    market: str,
    broker: str,
    quantity: int,
    order_routable: bool,
    average_cost: int | float,
    proposed_sell_price: int | float,
    profit_pct: int | float,
    resistance_near_pct: int | float | None,
    unresolved_open_actions: int,
) -> SingleShareExitEvaluation:
    """Evaluate the additive one-share full-exit *proposal* path.

    Scope and quantity are checked before the unresolved-action DEFER gate so
    unrelated positions do not get classified by a rule that does not apply to
    them. No broker, database, scheduler, or order-proposal mutation occurs.
    """

    doc = load_trading_policy()
    rule = doc.decision_rules.get("sell.single_share_exit")
    if not isinstance(rule, SingleShareExitDecisionRule):
        return SingleShareExitEvaluation("INELIGIBLE", "policy_rule_unavailable")

    if market not in rule.scope.markets:
        return SingleShareExitEvaluation("INELIGIBLE", "market_out_of_scope")
    if broker not in rule.scope.brokers:
        return SingleShareExitEvaluation("INELIGIBLE", "broker_out_of_scope")
    if rule.scope.order_routable_required and not order_routable:
        return SingleShareExitEvaluation("INELIGIBLE", "order_routable_false")
    if quantity != rule.conditions.quantity_eq:
        return SingleShareExitEvaluation("INELIGIBLE", "not_single_share_position")
    if unresolved_open_actions > rule.conditions.unresolved_open_actions_max:
        return SingleShareExitEvaluation("DEFER", "unresolved_open_action")
    if rule.conditions.resistance_reference_required and resistance_near_pct is None:
        return SingleShareExitEvaluation("INELIGIBLE", "no_resistance_reference")
    if resistance_near_pct is None or not (
        0 <= resistance_near_pct <= rule.conditions.resistance_near_pct_max
    ):
        return SingleShareExitEvaluation("INELIGIBLE", "resistance_not_ultra_near")
    if profit_pct < rule.conditions.profit_pct_min:
        return SingleShareExitEvaluation(
            "INELIGIBLE", "profit_below_provisional_minimum"
        )

    guard_spec = doc.thresholds.get(rule.conditions.min_sell_price_multiple_policy_key)
    try:
        avg = Decimal(str(average_cost))
        sell_price = Decimal(str(proposed_sell_price))
        guard_multiple = Decimal(str(guard_spec.value)) if guard_spec else Decimal(0)
    except (InvalidOperation, ValueError):
        return SingleShareExitEvaluation("INELIGIBLE", "invalid_price_evidence")
    if avg <= 0 or sell_price <= 0 or guard_multiple <= 0:
        return SingleShareExitEvaluation("INELIGIBLE", "invalid_price_evidence")
    if sell_price < avg * guard_multiple:
        return SingleShareExitEvaluation("INELIGIBLE", "loss_guard_not_met")

    proposal = rule.proposal
    return SingleShareExitEvaluation(
        outcome="PROPOSE",
        reason="single_share_profit_exit_eligible",
        action=proposal.action,
        sizing=proposal.sizing,
        approval=proposal.approval,
        auto_approve=proposal.auto_approve,
        execution=proposal.execution,
    )


_LOSS_CUT_MAX_SLIP_KEY = "sell.loss_cut_max_slip"
_LOSS_CUT_MAX_SLIP_DEFAULT = 0.02


def loss_cut_max_slip() -> float:
    """ROB-800 — max downward slip fraction for a sanctioned loss_cut limit sell.

    Code-enforced band magnitude sourced from config/trading_policy.yaml
    (sell.loss_cut_max_slip). Falls back to 0.02 if the key is absent so the
    guard stays fail-closed (a small band) rather than fail-open.
    """
    doc = load_trading_policy()
    spec = doc.thresholds.get(_LOSS_CUT_MAX_SLIP_KEY)
    if spec is None:
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    try:
        value = float(spec.value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    if not (0.0 < value < 0.5):
        return _LOSS_CUT_MAX_SLIP_DEFAULT
    return value
