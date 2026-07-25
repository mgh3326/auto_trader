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
    TradingPolicyDocument,
)
from app.services.single_share_exit_snapshot_service import (
    PRODUCER_CAPABILITY,
    PRODUCER_IDENTITY,
    ROSTER_CAPABILITY,
    ContextMode,
    ResistanceStrength,
    ValidatedSingleShareExitContext,
    executable_quote_price,
    is_validated_context,
    required_reader_capabilities,
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

    outcome: Literal["SHADOW_ELIGIBLE", "REPLAY_ELIGIBLE", "DEFER", "INELIGIBLE"]
    reason: str
    snapshot_id: str
    symbol: str
    broker: Literal["kis", "toss"]
    broker_account_id: str
    lot_id: str
    activation_state: Literal["shadow"] = "shadow"
    proposal_enabled: Literal[False] = False
    candidate_action: Literal["full_exit_at_far_resistance"] | None = None
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
    roster_id: str | None = None
    roster_version: str | None = None
    roster_hash: str | None = None
    expected_account_identities: tuple[str, ...] = ()
    observed_account_identities: tuple[str, ...] = ()
    producer_identity: str | None = None
    producer_capability: str | None = None
    quote_kind: str | None = None
    quote_venue: str | None = None
    quote_executable: bool | None = None
    quote_firm: bool | None = None


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
    decision_rules: dict[str, Any] = {}
    for key, spec in doc.decision_rules.items():
        if lane not in spec.lanes:
            continue
        if (
            isinstance(spec, SingleShareExitDecisionRule)
            and market not in spec.scope.markets
        ):
            continue
        decision_rules[key] = spec.model_dump(exclude={"lanes"})
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
    context: ValidatedSingleShareExitContext,
    *,
    outcome: Literal["SHADOW_ELIGIBLE", "REPLAY_ELIGIBLE", "DEFER", "INELIGIBLE"],
    reason: str,
    rule: SingleShareExitDecisionRule | None = None,
    average_cost: Decimal | None = None,
    current_quote: Decimal | None = None,
    profit_pct: Decimal | None = None,
    resistance_distance_pct: Decimal | None = None,
    normalized_source_families: tuple[str, ...] = (),
    expected_completed_krx_bar_date: dt.date | None = None,
    symbol_routable_sellable_quantity: Decimal | None = None,
    quote_age_seconds: Decimal | None = None,
) -> SingleShareExitEvaluation:
    proposal = rule.proposal if outcome == "SHADOW_ELIGIBLE" and rule else None
    expected_identities = tuple(
        f"{identity.broker.value}:{identity.broker_account_id}"
        for identity in context.expected_account_identities
    )
    observed_identities = tuple(
        f"{identity.broker.value}:{identity.broker_account_id}"
        for identity in context.observed_account_identities
    )
    return SingleShareExitEvaluation(
        outcome=outcome,
        reason=reason,
        snapshot_id=context.snapshot_id,
        symbol=context.target.symbol,
        broker=context.target.broker.value,
        broker_account_id=context.target.broker_account_id,
        lot_id=context.target.lot_id,
        candidate_action=proposal.action if proposal else None,
        sizing=proposal.sizing if proposal else None,
        approval=proposal.approval if proposal else None,
        auto_approve=proposal.auto_approve if proposal else False,
        execution=proposal.execution if proposal else None,
        average_cost=average_cost,
        symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
        current_quote=current_quote,
        quote_source=context.quote.source.value,
        quote_age_seconds=quote_age_seconds,
        resistance_price=(
            context.resistance.price if context.resistance is not None else None
        ),
        profit_pct=profit_pct,
        resistance_distance_pct=resistance_distance_pct,
        normalized_source_families=normalized_source_families,
        resistance_sources=(
            context.resistance.sources if context.resistance is not None else ()
        ),
        resistance_strength=(
            context.resistance.strength.value
            if context.resistance is not None
            else None
        ),
        quote_observed_at=context.quote.observed_at,
        resistance_computed_at=(
            context.resistance.computed_at if context.resistance is not None else None
        ),
        ohlcv_through_date=(
            context.resistance.ohlcv_through_date
            if context.resistance is not None
            else None
        ),
        expected_completed_krx_bar_date=expected_completed_krx_bar_date,
        roster_id=context.roster_id,
        roster_version=context.roster_version,
        roster_hash=context.roster_hash,
        expected_account_identities=expected_identities,
        observed_account_identities=observed_identities,
        producer_identity=context.producer_identity,
        producer_capability=context.producer_capability,
        quote_kind=context.quote.quote_kind.value,
        quote_venue=context.quote.venue.value,
        quote_executable=context.quote.executable,
        quote_firm=context.quote.firm,
    )


def _invalid_context_result(reason: str) -> SingleShareExitEvaluation:
    return SingleShareExitEvaluation(
        outcome="INELIGIBLE",
        reason=reason,
        snapshot_id="<invalid>",
        symbol="<invalid>",
        broker="kis",
        broker_account_id="<invalid>",
        lot_id="<invalid>",
    )


def _normalized_resistance_families(
    sources: tuple[str, ...], rule: SingleShareExitDecisionRule
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


def _evaluate_single_share_exit(
    context: ValidatedSingleShareExitContext,
    *,
    now: dt.datetime,
    eligible_outcome: Literal["SHADOW_ELIGIBLE", "REPLAY_ELIGIBLE"],
) -> SingleShareExitEvaluation:
    doc = load_trading_policy()
    rule = doc.decision_rules.get("sell.single_share_exit")
    if not isinstance(rule, SingleShareExitDecisionRule):
        return _single_share_result(
            context, outcome="INELIGIBLE", reason="policy_rule_unavailable"
        )
    if rule.activation_state != "shadow" or rule.proposal_enabled:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="policy_not_shadow_off",
            rule=rule,
        )
    if context.market not in rule.scope.markets:
        return _single_share_result(
            context, outcome="INELIGIBLE", reason="market_out_of_scope", rule=rule
        )
    if context.target.broker.value not in rule.scope.brokers:
        return _single_share_result(
            context, outcome="INELIGIBLE", reason="broker_out_of_scope", rule=rule
        )
    if (
        context.producer_identity != PRODUCER_IDENTITY
        or context.producer_capability != PRODUCER_CAPABILITY
        or context.roster_read_model_capability != ROSTER_CAPABILITY
        or context.reader_capabilities != required_reader_capabilities()
    ):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="authoritative_producer_capability_mismatch",
            rule=rule,
        )
    if context.roster_hash != context.derived_roster_hash:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="account_roster_hash_mismatch",
            rule=rule,
        )
    configured_identities = tuple(
        sorted(
            (account.identity for account in context.configured_accounts),
            key=lambda identity: (
                identity.broker.value,
                identity.broker_account_id,
            ),
        )
    )
    if (
        configured_identities != context.expected_account_identities
        or len(set(configured_identities)) != len(configured_identities)
        or not context.roster_is_exact
    ):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="expected_observed_account_roster_mismatch",
            rule=rule,
        )
    routable_brokers = {
        account.identity.broker.value
        for account in context.configured_accounts
        if account.order_routable
    }
    if not set(rule.scope.required_broker_inventory).issubset(routable_brokers):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="incomplete_kis_toss_routable_roster",
            rule=rule,
        )
    if context.resistance is None:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="no_resistance_reference",
            rule=rule,
        )
    required_strength = ResistanceStrength(rule.conditions.resistance_strength_min)
    if context.resistance.strength is not required_strength:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="resistance_strength_below_required",
            rule=rule,
        )
    if (
        context.quote.symbol != context.target.symbol
        or context.resistance.symbol != context.target.symbol
    ):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="inconsistent_snapshot_symbol",
            rule=rule,
        )
    quote = executable_quote_price(context.quote)
    if quote is None:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="quote_quality_not_executable",
            rule=rule,
        )

    timestamps_by_kind: dict[str, list[dt.datetime]] = {
        "quote": [context.quote.observed_at],
        "resistance": [context.resistance.computed_at],
        "holdings": [account.holdings_observed_at for account in context.accounts],
        "open_orders": [
            account.open_orders_observed_at for account in context.accounts
        ],
        "open_actions": [context.open_actions.observed_at],
        "captured_at": [context.captured_at],
    }
    all_timestamps = [
        timestamp
        for timestamps in timestamps_by_kind.values()
        for timestamp in timestamps
    ]
    if any(
        timestamp.tzinfo is None or timestamp.utcoffset() is None
        for timestamp in all_timestamps
    ):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="naive_evidence_timestamp",
            rule=rule,
            current_quote=quote,
        )
    timestamps_utc = [timestamp.astimezone(dt.UTC) for timestamp in all_timestamps]
    now_utc = now.astimezone(dt.UTC)
    if any(timestamp > now_utc for timestamp in timestamps_utc):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="future_evidence_timestamp",
            rule=rule,
            current_quote=quote,
        )
    pairwise_skew = max(timestamps_utc) - min(timestamps_utc)
    if pairwise_skew > dt.timedelta(seconds=rule.conditions.snapshot_max_skew_seconds):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="snapshot_pairwise_skew_exceeded",
            rule=rule,
            current_quote=quote,
        )
    age_limits = {
        "quote": rule.conditions.quote_max_age_seconds,
        "resistance": rule.conditions.resistance_max_age_seconds,
        "holdings": rule.conditions.holdings_max_age_seconds,
        "open_orders": rule.conditions.open_orders_max_age_seconds,
        "open_actions": rule.conditions.open_actions_max_age_seconds,
        "captured_at": rule.conditions.captured_at_max_age_seconds,
    }
    for kind, timestamps in timestamps_by_kind.items():
        oldest = min(timestamp.astimezone(dt.UTC) for timestamp in timestamps)
        if now_utc - oldest > dt.timedelta(seconds=age_limits[kind]):
            return _single_share_result(
                context,
                outcome="INELIGIBLE",
                reason=f"stale_{kind}",
                rule=rule,
                current_quote=quote,
                quote_age_seconds=Decimal(
                    str(
                        (
                            now_utc - context.quote.observed_at.astimezone(dt.UTC)
                        ).total_seconds()
                    )
                ),
            )
    quote_age_seconds = Decimal(
        str((now_utc - context.quote.observed_at.astimezone(dt.UTC)).total_seconds())
    )

    expected_bar_date = _expected_completed_krx_bar(now_utc)
    if expected_bar_date is None:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="expected_completed_krx_bar_unavailable",
            rule=rule,
            current_quote=quote,
            quote_age_seconds=quote_age_seconds,
        )
    if context.resistance.ohlcv_through_date != expected_bar_date:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="ohlcv_not_through_expected_completed_krx_bar",
            rule=rule,
            current_quote=quote,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    target_accounts = [
        account
        for account in context.configured_accounts
        if account.identity == context.target.account_identity
    ]
    if len(target_accounts) != 1 or (
        rule.scope.order_routable_required and not target_accounts[0].order_routable
    ):
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="target_account_not_routable",
            rule=rule,
            current_quote=quote,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    target_matches = [
        lot
        for account in context.accounts
        if account.identity == context.target.account_identity
        for lot in account.lots
        if lot.symbol == context.target.symbol and lot.lot_id == context.target.lot_id
    ]
    if len(target_matches) != 1:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="target_lot_identity_not_unique",
            rule=rule,
            current_quote=quote,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    target_lot = target_matches[0]
    required_quantity = Decimal(rule.conditions.symbol_routable_sellable_quantity_eq)
    target_is_single_sellable = target_lot.sellable_quantity == required_quantity
    if not target_is_single_sellable:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="target_account_lot_not_single_routable_sellable",
            rule=rule,
            average_cost=target_lot.average_cost,
            current_quote=quote,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    routable_identities = {
        account.identity
        for account in context.configured_accounts
        if account.order_routable
    }
    symbol_routable_sellable_quantity = sum(
        (
            lot.sellable_quantity
            for account in context.accounts
            if account.identity in routable_identities
            for lot in account.lots
            if lot.symbol == context.target.symbol
        ),
        start=Decimal(0),
    )
    if symbol_routable_sellable_quantity != required_quantity:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="symbol_routable_sellable_quantity_not_one",
            rule=rule,
            average_cost=target_lot.average_cost,
            current_quote=quote,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    same_symbol_open_orders = [
        order
        for account in context.accounts
        for order in account.open_orders
        if order.symbol == context.target.symbol
    ]
    if len(same_symbol_open_orders) > rule.conditions.same_symbol_open_orders_max:
        return _single_share_result(
            context,
            outcome="DEFER",
            reason="same_symbol_broker_open_order",
            rule=rule,
            average_cost=target_lot.average_cost,
            current_quote=quote,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    scoped_open_actions = [
        action
        for action in context.open_actions.actions
        if action.symbol == context.target.symbol
        and action.side == "sell"
        and action.broker_account_id == context.target.broker_account_id
    ]
    if len(scoped_open_actions) > rule.conditions.unresolved_open_actions_max:
        return _single_share_result(
            context,
            outcome="DEFER",
            reason="unresolved_scoped_open_action",
            rule=rule,
            average_cost=target_lot.average_cost,
            current_quote=quote,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )

    average_cost = target_lot.average_cost
    resistance = context.resistance.price
    guard_spec = doc.thresholds.get(rule.conditions.min_sell_price_multiple_policy_key)
    try:
        guard_multiple = Decimal(str(guard_spec.value)) if guard_spec else Decimal(0)
    except (InvalidOperation, ValueError):
        guard_multiple = Decimal(0)
    if guard_multiple <= 0:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="invalid_loss_guard_policy",
            rule=rule,
            average_cost=average_cost,
            current_quote=quote,
            symbol_routable_sellable_quantity=symbol_routable_sellable_quantity,
            quote_age_seconds=quote_age_seconds,
            expected_completed_krx_bar_date=expected_bar_date,
        )
    if quote < average_cost * guard_multiple:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="loss_guard_not_met",
            rule=rule,
            average_cost=average_cost,
            current_quote=quote,
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
        context.resistance.sources, rule
    )
    result_kwargs: dict[str, Any] = {
        "rule": rule,
        "average_cost": average_cost,
        "current_quote": quote,
        "profit_pct": profit_pct,
        "resistance_distance_pct": resistance_distance_pct,
        "normalized_source_families": normalized_families,
        "symbol_routable_sellable_quantity": symbol_routable_sellable_quantity,
        "quote_age_seconds": quote_age_seconds,
        "expected_completed_krx_bar_date": expected_bar_date,
    }
    if raw_profit_pct < Decimal(str(rule.conditions.profit_pct_min)):
        return _single_share_result(
            context,
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
            context,
            outcome="INELIGIBLE",
            reason="resistance_outside_far_band",
            **result_kwargs,
        )
    if len(normalized_families) < rule.conditions.resistance_source_family_min:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="insufficient_independent_resistance_families",
            **result_kwargs,
        )

    return _single_share_result(
        context,
        outcome=eligible_outcome,
        reason=(
            "proposal_disabled_shadow_candidate"
            if eligible_outcome == "SHADOW_ELIGIBLE"
            else "replay_candidate_no_live_eligibility"
        ),
        **result_kwargs,
    )


def evaluate_single_share_exit(
    context: ValidatedSingleShareExitContext,
) -> SingleShareExitEvaluation:
    """Evaluate only a live producer-issued context using the internal clock."""

    if not is_validated_context(context):
        return _invalid_context_result("unvalidated_producer_context")
    if context.mode is not ContextMode.LIVE:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="replay_context_not_live",
        )
    return _evaluate_single_share_exit(
        context,
        now=dt.datetime.now(dt.UTC),
        eligible_outcome="SHADOW_ELIGIBLE",
    )


def evaluate_single_share_exit_replay(
    context: ValidatedSingleShareExitContext,
) -> SingleShareExitEvaluation:
    """Deterministic offline seam; never returns live ``SHADOW_ELIGIBLE``."""

    if not is_validated_context(context):
        return _invalid_context_result("unvalidated_producer_context")
    if context.mode is not ContextMode.REPLAY:
        return _single_share_result(
            context,
            outcome="INELIGIBLE",
            reason="live_context_not_replay",
        )
    return _evaluate_single_share_exit(
        context,
        now=context.produced_at,
        eligible_outcome="REPLAY_ELIGIBLE",
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
