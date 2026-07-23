"""Loader for config/trading_policy.yaml — the single authoritative source
of trading judgment thresholds (ROB-646). Read-only; operator edits via PR."""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml

from app.schemas.trading_policy import (
    SingleShareExitDecisionRule,
    SingleShareExitEvidenceSnapshot,
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
    """Pure shadow-policy result; it neither persists nor proposes an order."""

    outcome: Literal["SHADOW_ELIGIBLE", "DEFER", "INELIGIBLE"]
    reason: str
    snapshot_id: str
    symbol: str
    broker: Literal["kis", "toss"]
    broker_account_id: str
    lot_id: str
    activation_state: Literal["shadow"] = "shadow"
    proposal_enabled: Literal[False] = False
    candidate_action: Literal["propose_full_account_lot_exit"] | None = None
    sizing: Literal["full_account_lot_exit"] | None = None
    approval: Literal["telegram_manual"] | None = None
    auto_approve: Literal[False] = False
    execution: Literal["proposal_only"] | None = None
    average_cost: Decimal | None = None
    symbol_routable_sellable_quantity: Decimal | None = None
    current_quote: Decimal | None = None
    quote_source: str | None = None
    quote_age_seconds: Decimal | None = None
    resistance_price: Decimal | None = None
    profit_pct: Decimal | None = None
    resistance_distance_pct: Decimal | None = None
    normalized_source_families: tuple[str, ...] = ()
    resistance_sources: tuple[str, ...] = ()
    resistance_strength: str | None = None
    quote_observed_at: dt.datetime | None = None
    resistance_computed_at: dt.datetime | None = None
    ohlcv_through_date: dt.date | None = None
    expected_completed_krx_bar_date: dt.date | None = None


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


def _single_share_result(
    evidence: SingleShareExitEvidenceSnapshot,
    *,
    outcome: Literal["SHADOW_ELIGIBLE", "DEFER", "INELIGIBLE"],
    reason: str,
    rule: SingleShareExitDecisionRule | None = None,
    average_cost: Decimal | None = None,
    profit_pct: Decimal | None = None,
    resistance_distance_pct: Decimal | None = None,
    normalized_source_families: tuple[str, ...] = (),
    expected_completed_krx_bar_date: dt.date | None = None,
    symbol_routable_sellable_quantity: Decimal | None = None,
    quote_age_seconds: Decimal | None = None,
) -> SingleShareExitEvaluation:
    proposal = rule.proposal if outcome == "SHADOW_ELIGIBLE" and rule else None
    return SingleShareExitEvaluation(
        outcome=outcome,
        reason=reason,
        snapshot_id=evidence.snapshot_id,
        symbol=evidence.target.symbol,
        broker=evidence.target.broker,
        broker_account_id=evidence.target.broker_account_id,
        lot_id=evidence.target.lot_id,
        candidate_action=proposal.action if proposal else None,
        sizing=proposal.sizing if proposal else None,
        approval=proposal.approval if proposal else None,
        auto_approve=proposal.auto_approve if proposal else False,
        execution=proposal.execution if proposal else None,
        average_cost=average_cost,
        symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
        current_quote=evidence.quote.price,
        quote_source=evidence.quote.source,
        quote_age_seconds=quote_age_seconds,
        resistance_price=evidence.resistance.price,
        profit_pct=profit_pct,
        resistance_distance_pct=resistance_distance_pct,
        normalized_source_families=normalized_source_families,
        resistance_sources=tuple(evidence.resistance.sources),
        resistance_strength=evidence.resistance.strength,
        quote_observed_at=evidence.quote.observed_at,
        resistance_computed_at=evidence.resistance.computed_at,
        ohlcv_through_date=evidence.resistance.ohlcv_through_date,
        expected_completed_krx_bar_date=expected_completed_krx_bar_date,
    )


def _as_aware_utc(value: dt.datetime) -> dt.datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(dt.UTC)


def _normalized_resistance_families(
    sources: list[str], rule: SingleShareExitDecisionRule
) -> tuple[str, ...]:
    normalization = rule.conditions.resistance_source_families
    volume_exact = {source.casefold() for source in normalization.volume_profile_exact}
    fib_prefixes = tuple(
        prefix.casefold() for prefix in normalization.fibonacci_prefixes
    )
    bollinger_prefixes = tuple(
        prefix.casefold() for prefix in normalization.bollinger_prefixes
    )
    families: set[str] = set()
    for raw_source in sources:
        source = raw_source.strip().casefold()
        if source in volume_exact:
            families.add("VOLUME_PROFILE")
        elif source.startswith(fib_prefixes):
            families.add("FIBONACCI")
        elif source.startswith(bollinger_prefixes):
            families.add("BOLLINGER")
    return tuple(sorted(families))


def _expected_completed_krx_bar(now: dt.datetime) -> dt.date | None:
    """Resolve the authoritative finalized KRX session without import side effects."""
    from app.services.daily_candles.read_service import last_final_session_kr

    return last_final_session_kr(now)


def evaluate_single_share_exit(
    evidence: SingleShareExitEvidenceSnapshot,
    *,
    evaluated_at: dt.datetime | None = None,
) -> SingleShareExitEvaluation:
    """Evaluate one bounded evidence snapshot in shadow mode only.

    Profit and resistance distance are recomputed from Decimal prices in the
    snapshot. KIS/Toss inventory and open-order evidence are caller-supplied
    read models, but completeness, identity, scope, timestamps, and aggregate
    quantities are checked here. This function has no broker, DB, scheduler,
    Telegram, proposal, or order side effects.
    """

    doc = load_trading_policy()
    rule = doc.decision_rules.get("sell.single_share_exit")
    if not isinstance(rule, SingleShareExitDecisionRule):
        return _single_share_result(
            evidence, outcome="INELIGIBLE", reason="policy_rule_unavailable"
        )
    if rule.activation_state != "shadow" or rule.proposal_enabled:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="policy_not_shadow_off",
            rule=rule,
        )
    if evidence.market not in rule.scope.markets:
        return _single_share_result(
            evidence, outcome="INELIGIBLE", reason="market_out_of_scope", rule=rule
        )
    if evidence.target.broker not in rule.scope.brokers:
        return _single_share_result(
            evidence, outcome="INELIGIBLE", reason="broker_out_of_scope", rule=rule
        )

    now = _as_aware_utc(evaluated_at or dt.datetime.now(dt.UTC))
    captured_at = _as_aware_utc(evidence.captured_at)
    if now is None or captured_at is None:
        return _single_share_result(
            evidence, outcome="INELIGIBLE", reason="naive_evidence_timestamp", rule=rule
        )

    nested_snapshot_ids = {
        evidence.quote.snapshot_id,
        evidence.resistance.snapshot_id,
        evidence.open_actions_snapshot_id,
        *(account.snapshot_id for account in evidence.accounts),
    }
    if nested_snapshot_ids != {evidence.snapshot_id}:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="inconsistent_snapshot_id",
            rule=rule,
        )
    if (
        evidence.quote.symbol != evidence.target.symbol
        or evidence.resistance.symbol != evidence.target.symbol
    ):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="inconsistent_snapshot_symbol",
            rule=rule,
        )

    required_brokers = set(rule.scope.required_broker_inventory)
    observed_brokers = {account.broker for account in evidence.accounts}
    if not required_brokers.issubset(observed_brokers):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="incomplete_kis_toss_inventory",
            rule=rule,
        )
    account_identities = [
        (account.broker, account.broker_account_id) for account in evidence.accounts
    ]
    if len(set(account_identities)) != len(account_identities):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="duplicate_broker_account_snapshot",
            rule=rule,
        )

    timestamp_values = [
        evidence.quote.observed_at,
        evidence.resistance.computed_at,
        evidence.open_actions_checked_at,
        *(
            timestamp
            for account in evidence.accounts
            for timestamp in (account.observed_at, account.open_orders_checked_at)
        ),
    ]
    timestamp_values_utc = [_as_aware_utc(value) for value in timestamp_values]
    if any(value is None for value in timestamp_values_utc):
        return _single_share_result(
            evidence, outcome="INELIGIBLE", reason="naive_evidence_timestamp", rule=rule
        )
    max_skew = dt.timedelta(seconds=rule.conditions.snapshot_max_skew_seconds)
    if captured_at > now or any(
        value is None or value > now or abs(value - captured_at) > max_skew
        for value in timestamp_values_utc
    ):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="stale_or_inconsistent_evidence",
            rule=rule,
        )
    quote_observed_at = _as_aware_utc(evidence.quote.observed_at)
    quote_age_seconds = (
        Decimal(str((now - quote_observed_at).total_seconds()))
        if quote_observed_at is not None
        else None
    )
    if quote_observed_at is None or now - quote_observed_at > dt.timedelta(
        seconds=rule.conditions.quote_max_age_seconds
    ):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="stale_quote",
            rule=rule,
            quote_age_seconds=quote_age_seconds,
        )

    expected_bar_date = _expected_completed_krx_bar(now)
    if expected_bar_date is None:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="expected_completed_krx_bar_unavailable",
            rule=rule,
        )
    if evidence.resistance.ohlcv_through_date != expected_bar_date:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="ohlcv_not_through_expected_completed_krx_bar",
            rule=rule,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    target_matches = [
        lot
        for account in evidence.accounts
        if account.broker == evidence.target.broker
        and account.broker_account_id == evidence.target.broker_account_id
        for lot in account.lots
        if lot.symbol == evidence.target.symbol and lot.lot_id == evidence.target.lot_id
    ]
    if len(target_matches) != 1:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="target_lot_identity_not_unique",
            rule=rule,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    target_lot = target_matches[0]
    required_quantity = Decimal(rule.conditions.symbol_routable_sellable_quantity_eq)
    target_is_routable = (
        not rule.scope.order_routable_required or target_lot.order_routable
    )
    target_is_single_sellable = target_lot.sellable_quantity == required_quantity
    if not target_is_routable or not target_is_single_sellable:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="target_account_lot_not_single_routable_sellable",
            rule=rule,
            average_cost=target_lot.average_cost,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    symbol_routable_sellable_quantity = sum(
        (
            lot.sellable_quantity
            for account in evidence.accounts
            for lot in account.lots
            if lot.symbol == evidence.target.symbol and lot.order_routable
        ),
        start=Decimal(0),
    )
    if symbol_routable_sellable_quantity != required_quantity:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="symbol_routable_sellable_quantity_not_one",
            rule=rule,
            average_cost=target_lot.average_cost,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    same_symbol_open_orders = [
        order
        for account in evidence.accounts
        for order in account.open_orders
        if order.symbol == evidence.target.symbol
    ]
    if len(same_symbol_open_orders) > rule.conditions.same_symbol_open_orders_max:
        return _single_share_result(
            evidence,
            outcome="DEFER",
            reason="same_symbol_broker_open_order",
            rule=rule,
            average_cost=target_lot.average_cost,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    scoped_open_actions = [
        action
        for action in evidence.open_actions
        if action.symbol == evidence.target.symbol
        and action.side == "sell"
        and action.broker_account_id == evidence.target.broker_account_id
    ]
    if len(scoped_open_actions) > rule.conditions.unresolved_open_actions_max:
        return _single_share_result(
            evidence,
            outcome="DEFER",
            reason="unresolved_scoped_open_action",
            rule=rule,
            average_cost=target_lot.average_cost,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    average_cost = target_lot.average_cost
    quote = evidence.quote.price
    resistance = evidence.resistance.price
    guard_spec = doc.thresholds.get(rule.conditions.min_sell_price_multiple_policy_key)
    try:
        guard_multiple = Decimal(str(guard_spec.value)) if guard_spec else Decimal(0)
    except (InvalidOperation, ValueError):
        guard_multiple = Decimal(0)
    if guard_multiple <= 0:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="invalid_loss_guard_policy",
            rule=rule,
            average_cost=average_cost,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    if quote < average_cost * guard_multiple:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="loss_guard_not_met",
            rule=rule,
            average_cost=average_cost,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    hundred = Decimal(100)
    raw_profit_pct = (quote - average_cost) / average_cost * hundred
    raw_resistance_distance_pct = (resistance - quote) / quote * hundred
    profit_pct = raw_profit_pct.quantize(Decimal("0.0001"))
    resistance_distance_pct = raw_resistance_distance_pct.quantize(Decimal("0.0001"))
    normalized_families = _normalized_resistance_families(
        evidence.resistance.sources, rule
    )
    result_kwargs: dict[str, Any] = {
        "rule": rule,
        "average_cost": average_cost,
        "profit_pct": profit_pct,
        "resistance_distance_pct": resistance_distance_pct,
        "normalized_source_families": normalized_families,
        "symbol_routable_sellable_quantity": symbol_routable_sellable_quantity,
        "quote_age_seconds": quote_age_seconds,
        "expected_completed_krx_bar_date": expected_bar_date,
    }
    if raw_profit_pct < Decimal(str(rule.conditions.profit_pct_min)):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="profit_below_provisional_minimum",
            **result_kwargs,
        )
    if not (
        raw_resistance_distance_pct
        > Decimal(str(rule.conditions.resistance_distance_pct_min_exclusive))
        and raw_resistance_distance_pct
        <= Decimal(str(rule.conditions.resistance_distance_pct_max))
    ):
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="resistance_outside_far_band",
            **result_kwargs,
        )
    if len(normalized_families) < rule.conditions.resistance_source_family_min:
        return _single_share_result(
            evidence,
            outcome="INELIGIBLE",
            reason="insufficient_independent_resistance_families",
            **result_kwargs,
        )

    return _single_share_result(
        evidence,
        outcome="SHADOW_ELIGIBLE",
        reason="proposal_disabled_shadow_candidate",
        **result_kwargs,
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
