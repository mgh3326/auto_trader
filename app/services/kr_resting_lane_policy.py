"""Pure deterministic evaluator for the provisional ROB-1037 KR resting lane.

No repository, broker, network, scheduler, or mutation surface is imported.
Callers must assemble typed evidence elsewhere. The shipped policy is shadow
only, so PLACE verdicts carry a non-executable shadow order while
proposal_allowed remains false.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Iterable

from app.schemas.kr_resting_lane import (
    KRConcurrencyState,
    KRDirectionState,
    KREvaluationStage,
    KREvidenceStatus,
    KRGateState,
    KRLevelEvidence,
    KRLevelKind,
    KRLevelSource,
    KRMarketDataState,
    KRNearResistanceAction,
    KROpenActionState,
    KRRestingLaneDecision,
    KRRestingLaneEvidence,
    KRRestingLanePolicy,
    KRRestingReason,
    KRRestingSide,
    KRRestingVerdict,
    KRRoutableAccountMode,
    KRSelectedLevel,
    KRShadowOrderIntent,
    KRSourceFamily,
)
from app.services.research_canonical_hash import canonical_sha256


_FIBONACCI_SOURCES = frozenset(
    {
        KRLevelSource.FIB_0,
        KRLevelSource.FIB_23_6,
        KRLevelSource.FIB_38_2,
        KRLevelSource.FIB_50,
        KRLevelSource.FIB_61_8,
        KRLevelSource.FIB_78_6,
        KRLevelSource.FIB_100,
        KRLevelSource.FIB_RATIO_0,
        KRLevelSource.FIB_RATIO_0_236,
        KRLevelSource.FIB_RATIO_0_382,
        KRLevelSource.FIB_RATIO_0_5,
        KRLevelSource.FIB_RATIO_0_618,
        KRLevelSource.FIB_RATIO_0_786,
        KRLevelSource.FIB_RATIO_1,
    }
)
_BOLLINGER_SOURCES = frozenset(
    {
        KRLevelSource.BB_LOWER,
        KRLevelSource.BB_MIDDLE,
        KRLevelSource.BB_UPPER,
    }
)
_VOLUME_PROFILE_SOURCES = frozenset(
    {
        KRLevelSource.VOLUME_POC,
        KRLevelSource.VOLUME_VALUE_AREA_HIGH,
        KRLevelSource.VOLUME_VALUE_AREA_LOW,
    }
)

_SOURCE_FAMILY = {
    **{source: KRSourceFamily.FIBONACCI for source in _FIBONACCI_SOURCES},
    **{source: KRSourceFamily.BOLLINGER for source in _BOLLINGER_SOURCES},
    **{
        source: KRSourceFamily.VOLUME_PROFILE
        for source in _VOLUME_PROFILE_SOURCES
    },
}
if set(_SOURCE_FAMILY) != set(KRLevelSource):  # pragma: no cover
    raise RuntimeError("every typed KR level source must map to exactly one family")

_REASON_ORDER = {
    reason: index for index, reason in enumerate(KRRestingReason)
}
_PLACE_VERDICTS = frozenset(
    {
        KRRestingVerdict.PLACE_MODERATE_SUPPORT,
        KRRestingVerdict.PLACE_DEEP_SUPPORT,
        KRRestingVerdict.PLACE_NEAR_RESISTANCE_TRIM,
        KRRestingVerdict.PLACE_FAR_RESISTANCE,
        KRRestingVerdict.FULL_EXIT_SINGLE_SHARE_AT_FAR_RESISTANCE,
    }
)


def normalized_source_families(
    sources: Iterable[KRLevelSource],
) -> tuple[KRSourceFamily, ...]:
    """Collapse correlated source labels into independent source families."""

    return tuple(sorted({_SOURCE_FAMILY[source] for source in sources}, key=str))


def _ordered_reasons(
    reasons: Iterable[KRRestingReason],
) -> tuple[KRRestingReason, ...]:
    return tuple(sorted(set(reasons), key=_REASON_ORDER.__getitem__))


def _distance_pct(level_price: Decimal, current_price: Decimal) -> Decimal:
    return (level_price - current_price) / current_price * Decimal("100")


def _snap_to_tick(
    price: Decimal, tick_size: Decimal, *, side: KRRestingSide
) -> Decimal:
    rounding = ROUND_FLOOR if side is KRRestingSide.BUY else ROUND_CEILING
    units = (price / tick_size).to_integral_value(rounding=rounding)
    return units * tick_size


def _selected_level(
    level: KRLevelEvidence, current_price: Decimal
) -> KRSelectedLevel:
    families = normalized_source_families(level.sources)
    return KRSelectedLevel(
        level_id=level.level_id,
        price=level.price,
        distance_pct=_distance_pct(level.price, current_price),
        source_families=families,
        family_count=len(families),
    )


def _direction_state(evidence: KRRestingLaneEvidence) -> KRDirectionState:
    sides = {order.side for order in evidence.reconciliation.open_orders}
    if sides == {KRRestingSide.BUY, KRRestingSide.SELL}:
        return KRDirectionState.CONFLICT
    if sides == {KRRestingSide.BUY}:
        return KRDirectionState.ACCUMULATING
    if sides == {KRRestingSide.SELL}:
        return KRDirectionState.DISTRIBUTING
    return KRDirectionState.NONE


def _account_reasons(
    evidence: KRRestingLaneEvidence, policy: KRRestingLanePolicy
) -> tuple[KRRestingReason, ...]:
    allowed = {mode.value for mode in policy.account_scope.allowed_account_modes}
    reasons: list[KRRestingReason] = []
    if evidence.account.account_mode.value not in allowed:
        reasons.append(KRRestingReason.ACCOUNT_MODE_UNSUPPORTED)
    if not evidence.account.order_routable:
        reasons.append(KRRestingReason.ACCOUNT_NOT_ROUTABLE)
    return _ordered_reasons(reasons)


def _lifecycle_reasons(
    evidence: KRRestingLaneEvidence, policy: KRRestingLanePolicy
) -> tuple[KRRestingReason, ...]:
    reconciliation = evidence.reconciliation
    scans = {scan.account_mode: scan for scan in reconciliation.broker_scans}
    required = {
        KRRoutableAccountMode.KIS_LIVE,
        KRRoutableAccountMode.TOSS_LIVE,
    }
    reasons: list[KRRestingReason] = []
    if set(scans) != required:
        reasons.append(KRRestingReason.BROKER_SCAN_MISSING)
    if any(not scan.open_complete for scan in scans.values()):
        reasons.append(KRRestingReason.BROKER_OPEN_SCAN_INCOMPLETE)
    if any(not scan.closed_complete for scan in scans.values()):
        reasons.append(KRRestingReason.BROKER_CLOSED_SCAN_INCOMPLETE)
    if any(not scan.pagination_complete for scan in scans.values()):
        reasons.append(KRRestingReason.BROKER_PAGINATION_INCOMPLETE)
    if not reconciliation.ledger_matches_broker:
        reasons.append(KRRestingReason.LEDGER_BROKER_MISMATCH)
    if reconciliation.ghost_resting_present:
        reasons.append(KRRestingReason.GHOST_RESTING_PRESENT)
    if reconciliation.day_expiry_unreconciled:
        reasons.append(KRRestingReason.DAY_EXPIRY_UNRECONCILED)

    if evidence.stage is KREvaluationStage.APPROVAL:
        requery_at = reconciliation.approval_requery_at
        ttl = policy.freshness.final_revalidation_quote_ttl_seconds
        if (
            requery_at is None
            or requery_at > evidence.evaluated_at
            or (evidence.evaluated_at - requery_at).total_seconds() > ttl
        ):
            reasons.append(KRRestingReason.APPROVAL_REQUERY_MISSING)
        if (
            reconciliation.symbol_reservation_state
            is not KRConcurrencyState.ACQUIRED
        ):
            reasons.append(KRRestingReason.SYMBOL_RESERVATION_MISSING)
        if reconciliation.symbol_lock_state is not KRConcurrencyState.ACQUIRED:
            reasons.append(KRRestingReason.SYMBOL_LOCK_MISSING)
    return _ordered_reasons(reasons)


def _freshness_reasons(
    evidence: KRRestingLaneEvidence, policy: KRRestingLanePolicy
) -> tuple[KRRestingReason, ...]:
    reasons: list[KRRestingReason] = []
    quote = evidence.quote
    if quote is None:
        reasons.append(KRRestingReason.QUOTE_MISSING)
    else:
        if quote.market_data_state is not KRMarketDataState.LIVE:
            reasons.append(KRRestingReason.QUOTE_NOT_LIVE)
        age = (evidence.evaluated_at - quote.captured_at).total_seconds()
        ttl = (
            policy.freshness.discovery_quote_ttl_seconds
            if evidence.stage is KREvaluationStage.DISCOVERY
            else policy.freshness.final_revalidation_quote_ttl_seconds
        )
        if age < 0:
            reasons.append(KRRestingReason.QUOTE_FROM_FUTURE)
        elif age > ttl:
            reasons.append(KRRestingReason.QUOTE_STALE)

    level_set = evidence.level_set
    if level_set.status is not KREvidenceStatus.COMPLETE:
        reasons.append(KRRestingReason.LEVEL_SET_INCOMPLETE)
    if level_set.computed_at > evidence.evaluated_at:
        reasons.append(KRRestingReason.LEVEL_COMPUTED_IN_FUTURE)
    for level in level_set.levels:
        if level.ohlcv_through != level_set.expected_completed_bar_date:
            reasons.append(KRRestingReason.EXPECTED_BASELINE_MISMATCH)
        if (
            level.snapshot_id != level_set.snapshot_id
            or level.computed_at != level_set.computed_at
        ):
            reasons.append(KRRestingReason.LEVELS_MIXED_VINTAGE)
        if level.computed_at > evidence.evaluated_at:
            reasons.append(KRRestingReason.LEVEL_COMPUTED_IN_FUTURE)
    return _ordered_reasons(reasons)


def _policy_reasons(
    evidence: KRRestingLaneEvidence,
    policy: KRRestingLanePolicy,
    *,
    policy_document_version: str,
    policy_content_hash: str,
) -> tuple[KRRestingReason, ...]:
    stamp = evidence.policy_stamp
    reasons: list[KRRestingReason] = []
    if stamp.document_version != policy_document_version:
        reasons.append(KRRestingReason.POLICY_DOCUMENT_VERSION_MISMATCH)
    if stamp.content_hash != policy_content_hash:
        reasons.append(KRRestingReason.POLICY_CONTENT_HASH_MISMATCH)
    if stamp.lane_policy_version != policy.policy_version:
        reasons.append(KRRestingReason.LANE_POLICY_VERSION_MISMATCH)
    return _ordered_reasons(reasons)


def _finish(
    *,
    evidence: KRRestingLaneEvidence,
    policy: KRRestingLanePolicy,
    policy_document_version: str,
    policy_content_hash: str,
    verdict: KRRestingVerdict,
    reasons: Iterable[KRRestingReason],
    direction_state: KRDirectionState,
    selected_level: KRSelectedLevel | None = None,
    shadow_order: KRShadowOrderIntent | None = None,
) -> KRRestingLaneDecision:
    ordered_reasons = _ordered_reasons(reasons)
    if not ordered_reasons:
        raise ValueError("a deterministic verdict requires at least one reason")
    evidence_hash = canonical_sha256(evidence.model_dump(mode="python"))
    decision_payload = {
        "evidence_hash": evidence_hash,
        "policy_document_version": policy_document_version,
        "policy_content_hash": policy_content_hash,
        "lane_policy_version": policy.policy_version,
        "calibration_version": policy.calibration_version,
        "verdict": verdict.value,
        "reason_codes": [reason.value for reason in ordered_reasons],
        "direction_state": direction_state.value,
        "selected_level": (
            selected_level.model_dump(mode="python")
            if selected_level is not None
            else None
        ),
        "shadow_order": (
            shadow_order.model_dump(mode="python")
            if shadow_order is not None
            else None
        ),
    }
    is_place = verdict in _PLACE_VERDICTS
    return KRRestingLaneDecision(
        decision_id=f"kr-resting-{canonical_sha256(decision_payload)[:32]}",
        evidence_hash=evidence_hash,
        policy_document_version=policy_document_version,
        policy_content_hash=policy_content_hash,
        lane_policy_version=policy.policy_version,
        calibration_version=policy.calibration_version,
        calibration_status=policy.activation.calibration_status,
        verdict=verdict,
        primary_reason=ordered_reasons[0],
        reason_codes=ordered_reasons,
        direction_state=direction_state,
        selected_level=selected_level,
        shadow_order=shadow_order,
        shadow_place_candidate=is_place,
        rung_count=1 if is_place else 0,
        evidence=evidence,
    )


def _evaluate_buy(
    *,
    evidence: KRRestingLaneEvidence,
    policy: KRRestingLanePolicy,
    policy_document_version: str,
    policy_content_hash: str,
    direction_state: KRDirectionState,
) -> KRRestingLaneDecision:
    common = {
        "evidence": evidence,
        "policy": policy,
        "policy_document_version": policy_document_version,
        "policy_content_hash": policy_content_hash,
        "direction_state": direction_state,
    }
    position = evidence.position
    if (
        position.status is not KREvidenceStatus.COMPLETE
        or position.symbol_total_qty is None
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_POLICY_GATE,
            reasons=[KRRestingReason.POSITION_EVIDENCE_INCOMPLETE],
        )
    if position.symbol_total_qty > 0:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_POLICY_GATE,
            reasons=[KRRestingReason.ADD_SIZING_UNDEFINED],
        )

    assert evidence.quote is not None
    current_price = evidence.quote.price
    supports = [
        _selected_level(level, current_price)
        for level in evidence.level_set.levels
        if level.kind is KRLevelKind.SUPPORT and level.price < current_price
    ]
    if not supports:
        return _finish(
            **common,
            verdict=KRRestingVerdict.HOLD_NO_SUPPORT,
            reasons=[KRRestingReason.NO_SUPPORT_BELOW_MARKET],
        )

    action_band = [
        selected
        for selected in supports
        if policy.bands.moderate_support.contains(selected.distance_pct)
        or policy.bands.deep_support.contains(selected.distance_pct)
    ]
    if not action_band:
        return _finish(
            **common,
            verdict=KRRestingVerdict.HOLD_NO_SUPPORT,
            reasons=[KRRestingReason.NO_SUPPORT_IN_ACTION_BAND],
        )

    levels_by_id = {level.level_id: level for level in evidence.level_set.levels}
    if any(
        levels_by_id[selected.level_id].provenance_status
        is not KREvidenceStatus.COMPLETE
        for selected in action_band
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[KRRestingReason.SUPPORT_PROVENANCE_INCOMPLETE],
        )

    minimum_families = policy.source_families.minimum_independent_family_count
    eligible = [
        selected
        for selected in action_band
        if selected.family_count >= minimum_families
    ]
    if not eligible:
        return _finish(
            **common,
            verdict=KRRestingVerdict.HOLD_NO_SUPPORT,
            reasons=[KRRestingReason.SUPPORT_CONFLUENCE_INSUFFICIENT],
        )
    selected = min(
        eligible,
        key=lambda item: (
            -item.family_count,
            abs(item.distance_pct),
            -item.price,
            tuple(family.value for family in item.source_families),
            item.level_id,
        ),
    )

    tick_size = evidence.microstructure.tick_size
    limit_price = _snap_to_tick(
        selected.price, tick_size, side=KRRestingSide.BUY
    )
    executable_distance = _distance_pct(limit_price, current_price)
    if not (
        policy.bands.moderate_support.contains(executable_distance)
        or policy.bands.deep_support.contains(executable_distance)
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_POLICY_GATE,
            reasons=[KRRestingReason.TICK_SNAP_OUTSIDE_ACTION_BAND],
            selected_level=selected,
        )

    minimum = Decimal(policy.buy_sizing.new_entry_min_notional_krw)
    maximum = Decimal(policy.buy_sizing.new_entry_max_notional_krw)
    quantity = int((minimum / limit_price).to_integral_value(rounding=ROUND_CEILING))
    quantity = max(quantity, 1)
    notional = limit_price * quantity
    if notional < minimum or notional > maximum:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_POLICY_GATE,
            reasons=[KRRestingReason.NOTIONAL_POLICY_UNSATISFIED],
            selected_level=selected,
        )

    cash = evidence.cash
    if (
        cash is None
        or cash.status is not KREvidenceStatus.COMPLETE
        or cash.orderable_cash_krw is None
        or cash.reserved_cash_krw is None
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_CASH,
            reasons=[KRRestingReason.CASH_EVIDENCE_INCOMPLETE],
            selected_level=selected,
        )
    effective_cash = cash.orderable_cash_krw - cash.reserved_cash_krw
    if effective_cash < notional:
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_CASH,
            reasons=[KRRestingReason.CASH_INSUFFICIENT],
            selected_level=selected,
        )

    if policy.bands.moderate_support.contains(selected.distance_pct):
        verdict = KRRestingVerdict.PLACE_MODERATE_SUPPORT
        reason = KRRestingReason.SUPPORT_MODERATE_BAND
    else:
        verdict = KRRestingVerdict.PLACE_DEEP_SUPPORT
        reason = KRRestingReason.SUPPORT_DEEP_BAND
    return _finish(
        **common,
        verdict=verdict,
        reasons=[reason],
        selected_level=selected,
        shadow_order=KRShadowOrderIntent(
            side=KRRestingSide.BUY,
            limit_price=limit_price,
            quantity=quantity,
            notional_krw=notional,
        ),
    )


def _composite_reason(
    evidence: KRRestingLaneEvidence,
) -> KRRestingReason | None:
    composite = evidence.composite
    if composite is None or composite.status is not KREvidenceStatus.COMPLETE:
        return KRRestingReason.COMPOSITE_EVIDENCE_INCOMPLETE
    if composite.gate_state is KRGateState.FAIL:
        return KRRestingReason.COMPOSITE_GATE_FAILED
    if composite.gate_state is KRGateState.UNKNOWN:
        return KRRestingReason.COMPOSITE_GATE_UNKNOWN
    return None


def _evaluate_sell(
    *,
    evidence: KRRestingLaneEvidence,
    policy: KRRestingLanePolicy,
    policy_document_version: str,
    policy_content_hash: str,
    direction_state: KRDirectionState,
) -> KRRestingLaneDecision:
    common = {
        "evidence": evidence,
        "policy": policy,
        "policy_document_version": policy_document_version,
        "policy_content_hash": policy_content_hash,
        "direction_state": direction_state,
    }
    position = evidence.position
    if (
        position.status is not KREvidenceStatus.COMPLETE
        or position.selected_account_qty is None
        or position.sellable_qty is None
        or position.avg_cost is None
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[KRRestingReason.POSITION_EVIDENCE_INCOMPLETE],
        )
    if position.selected_account_qty < 1 or position.sellable_qty < 1:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[KRRestingReason.NO_SELLABLE_POSITION],
        )

    assert evidence.quote is not None
    current_price = evidence.quote.price
    resistances = [
        _selected_level(level, current_price)
        for level in evidence.level_set.levels
        if level.kind is KRLevelKind.RESISTANCE and level.price > current_price
    ]
    if not resistances:
        return _finish(
            **common,
            verdict=KRRestingVerdict.HOLD_NO_RESISTANCE,
            reasons=[KRRestingReason.NO_RESISTANCE_ABOVE_MARKET],
        )

    normal_floor = position.avg_cost * policy.sell.normal_sell_floor_multiple
    above_guard = [
        selected for selected in resistances if selected.price >= normal_floor
    ]
    if not above_guard:
        return _finish(
            **common,
            verdict=KRRestingVerdict.HOLD_LOSS_GUARD,
            reasons=[
                KRRestingReason.NO_RESISTANCE_ABOVE_GUARD,
                KRRestingReason.LOSS_GUARD_NOT_CLEARED,
            ],
        )

    levels_by_id = {level.level_id: level for level in evidence.level_set.levels}
    if any(
        levels_by_id[selected.level_id].provenance_status
        is not KREvidenceStatus.COMPLETE
        for selected in above_guard
    ):
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[KRRestingReason.RESISTANCE_PROVENANCE_INCOMPLETE],
        )

    minimum_families = policy.source_families.minimum_independent_family_count
    eligible = [
        selected
        for selected in above_guard
        if selected.family_count >= minimum_families
    ]
    if not eligible:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[KRRestingReason.RESISTANCE_CONFLUENCE_INSUFFICIENT],
        )
    selected = min(eligible, key=lambda item: (item.price, item.level_id))
    limit_price = _snap_to_tick(
        selected.price,
        evidence.microstructure.tick_size,
        side=KRRestingSide.SELL,
    )

    if selected.distance_pct > policy.bands.far_resistance.upper_pct:
        return _finish(
            **common,
            verdict=KRRestingVerdict.WATCH_RESISTANCE,
            reasons=[KRRestingReason.RESISTANCE_TOO_FAR],
            selected_level=selected,
        )

    composite_reason = _composite_reason(evidence)
    if composite_reason is not None:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
            reasons=[composite_reason],
            selected_level=selected,
        )
    assert evidence.composite is not None

    if selected.distance_pct <= policy.bands.near_resistance_upper_inclusive_pct:
        if position.selected_account_qty == 1:
            return _finish(
                **common,
                verdict=KRRestingVerdict.WATCH_RESISTANCE,
                reasons=[KRRestingReason.SINGLE_SHARE_NEAR_NOT_FULL_EXIT],
                selected_level=selected,
            )
        if (
            evidence.composite.near_resistance_action
            is KRNearResistanceAction.UNKNOWN
        ):
            return _finish(
                **common,
                verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
                reasons=[KRRestingReason.NEAR_RESISTANCE_ACTION_UNKNOWN],
                selected_level=selected,
            )
        if (
            evidence.composite.near_resistance_action
            is not KRNearResistanceAction.PLACE_TRIM
        ):
            return _finish(
                **common,
                verdict=KRRestingVerdict.WATCH_RESISTANCE,
                reasons=[KRRestingReason.RESISTANCE_NEAR_WATCH],
                selected_level=selected,
            )
        return _finish(
            **common,
            verdict=KRRestingVerdict.PLACE_NEAR_RESISTANCE_TRIM,
            reasons=[KRRestingReason.RESISTANCE_NEAR_PLACE],
            selected_level=selected,
            shadow_order=KRShadowOrderIntent(
                side=KRRestingSide.SELL,
                limit_price=limit_price,
                quantity=1,
                notional_krw=limit_price,
                normal_sell_floor=normal_floor,
            ),
        )

    if policy.bands.far_resistance.contains(selected.distance_pct):
        if position.selected_account_qty >= 2:
            return _finish(
                **common,
                verdict=KRRestingVerdict.PLACE_FAR_RESISTANCE,
                reasons=[KRRestingReason.RESISTANCE_FAR_BAND],
                selected_level=selected,
                shadow_order=KRShadowOrderIntent(
                    side=KRRestingSide.SELL,
                    limit_price=limit_price,
                    quantity=policy.sell.multi_share_far_quantity,
                    notional_krw=(
                        limit_price * policy.sell.multi_share_far_quantity
                    ),
                    normal_sell_floor=normal_floor,
                ),
            )
        if not policy.sell.single_share_far_full_exit_enabled:
            return _finish(
                **common,
                verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
                reasons=[KRRestingReason.FULL_EXIT_POLICY_DISABLED],
                selected_level=selected,
            )
        if evidence.composite.open_action_state in {
            KROpenActionState.ACTIVE,
            KROpenActionState.UNKNOWN,
        }:
            return _finish(
                **common,
                verdict=KRRestingVerdict.DEFER_COMPOSITE_EVIDENCE,
                reasons=[KRRestingReason.OPEN_ACTION_UNRESOLVED],
                selected_level=selected,
            )
        return _finish(
            **common,
            verdict=KRRestingVerdict.FULL_EXIT_SINGLE_SHARE_AT_FAR_RESISTANCE,
            reasons=[KRRestingReason.SINGLE_SHARE_FULL_EXIT],
            selected_level=selected,
            shadow_order=KRShadowOrderIntent(
                side=KRRestingSide.SELL,
                limit_price=limit_price,
                quantity=1,
                notional_krw=limit_price,
                normal_sell_floor=normal_floor,
            ),
        )

    return _finish(
        **common,
        verdict=KRRestingVerdict.WATCH_RESISTANCE,
        reasons=[KRRestingReason.RESISTANCE_TOO_FAR],
        selected_level=selected,
    )


def evaluate_kr_resting_lane(
    evidence: KRRestingLaneEvidence,
    *,
    policy: KRRestingLanePolicy,
    policy_document_version: str,
    policy_content_hash: str,
) -> KRRestingLaneDecision:
    """Evaluate one typed evidence envelope without external I/O."""

    direction_state = _direction_state(evidence)
    common = {
        "evidence": evidence,
        "policy": policy,
        "policy_document_version": policy_document_version,
        "policy_content_hash": policy_content_hash,
        "direction_state": direction_state,
    }

    account_reasons = _account_reasons(evidence, policy)
    if account_reasons:
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_ACCOUNT_SCOPE,
            reasons=account_reasons,
        )
    if evidence.account.session_state.value != "krx_regular":
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_SESSION,
            reasons=[KRRestingReason.SESSION_NOT_REGULAR],
        )

    lifecycle_reasons = _lifecycle_reasons(evidence, policy)
    if lifecycle_reasons:
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_LIFECYCLE_UNRECONCILED,
            reasons=lifecycle_reasons,
        )

    freshness_reasons = _freshness_reasons(evidence, policy)
    if freshness_reasons:
        stale_verdict = (
            KRRestingVerdict.BLOCK_STALE_SUPPORT
            if evidence.side is KRRestingSide.BUY
            else KRRestingVerdict.BLOCK_STALE_RESISTANCE
        )
        return _finish(
            **common,
            verdict=stale_verdict,
            reasons=freshness_reasons,
        )

    opposite_side = (
        KRRestingSide.SELL
        if evidence.side is KRRestingSide.BUY
        else KRRestingSide.BUY
    )
    if any(
        order.side is opposite_side
        for order in evidence.reconciliation.open_orders
    ):
        reason = (
            KRRestingReason.OPPOSITE_SELL_OPEN
            if opposite_side is KRRestingSide.SELL
            else KRRestingReason.OPPOSITE_BUY_OPEN
        )
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_OPPOSITE_PENDING,
            reasons=[reason],
        )
    if any(
        order.side is evidence.side
        for order in evidence.reconciliation.open_orders
    ):
        reason = (
            KRRestingReason.SAME_SIDE_BUY_RESTING_OPEN
            if evidence.side is KRRestingSide.BUY
            else KRRestingReason.SAME_SIDE_SELL_RESTING_OPEN
        )
        return _finish(
            **common,
            verdict=KRRestingVerdict.BLOCK_DUPLICATE_RESTING,
            reasons=[reason],
        )

    policy_reasons = _policy_reasons(
        evidence,
        policy,
        policy_document_version=policy_document_version,
        policy_content_hash=policy_content_hash,
    )
    if policy_reasons:
        return _finish(
            **common,
            verdict=KRRestingVerdict.DEFER_POLICY_GATE,
            reasons=policy_reasons,
        )

    if evidence.side is KRRestingSide.BUY:
        return _evaluate_buy(**common)
    return _evaluate_sell(**common)


__all__ = ["evaluate_kr_resting_lane", "normalized_source_families"]
