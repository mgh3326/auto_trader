"""§176차 — the advisory dynamic deployment cap (``buy.deployment_cap``).

Pure and deterministic: it reads the coefficient from the authoritative policy
and does arithmetic. It performs no I/O, never blocks anything, and has no
opinion about existing deployments.

Two properties are load-bearing and are the reason this lives in its own
module rather than inline at a call site:

``forward only``
    The denominator is *orderable* cash, so it shrinks every time an order
    fills. An already-placed deployment can therefore find itself above a
    later cap without anyone deciding anything. The policy declares
    ``retroactive_violation: false``; this module honours that by only ever
    measuring a *planned new* deployment against the cap, and by reporting
    existing deployment as context rather than as a breach.

``missing parking means zero``
    ``cash_yields`` in the policy is a rate table with no balances, and the
    only balance store in this repo is the operator's ``manual_cash`` user
    setting. When that is absent or stale the parking term is 0 — never
    estimated — so the emitted cap is a conservative lower bound, reported as
    such through ``parking_balance_source``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

DEPLOYMENT_CAP_RULE_KEY = "buy.deployment_cap"
DEPLOYMENT_CAP_TIER_ID = "deployment_cap"

# Closed vocabulary. A reader that does not recognise a value must treat it as
# "no advisory", never as "within cap".
STATUS_WITHIN = "within_cap"
STATUS_EXCEEDS = "exceeds_cap"
STATUS_UNAVAILABLE = "unavailable"

PARKING_SOURCE_MANUAL_CASH = "manual_cash"
PARKING_SOURCE_ABSENT = "absent_treated_as_zero"
PARKING_SOURCE_STALE = "stale_treated_as_zero"


class DeploymentCapPolicyError(ValueError):
    """The policy does not carry a usable ``buy.deployment_cap`` contract."""


@dataclass(frozen=True, slots=True)
class DeploymentCapPolicy:
    coefficient_pct: Decimal
    recalculation_cadence: str
    retroactive_violation: bool
    blocks_proposal: bool


def load_deployment_cap_policy(policy: Any | None = None) -> DeploymentCapPolicy:
    """Read the frozen cap contract, failing closed on any drift.

    Fail-closed here means *raise*, not "assume 45": a caller that cannot read
    the policy must emit an ``unavailable`` advisory rather than invent a
    ceiling.
    """

    document = policy if policy is not None else load_trading_policy()
    try:
        rule = document.decision_rules[DEPLOYMENT_CAP_RULE_KEY]
    except KeyError as exc:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} is missing from the trading policy"
        ) from exc
    tiers = [
        tier for tier in getattr(rule, "tiers", []) if tier.id == DEPLOYMENT_CAP_TIER_ID
    ]
    if len(tiers) != 1:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} must declare exactly one "
            f"{DEPLOYMENT_CAP_TIER_ID} tier"
        )
    conditions = tiers[0].conditions
    coefficient = conditions.get("coefficient_pct")
    if not isinstance(coefficient, int | float) or isinstance(coefficient, bool):
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} requires a numeric coefficient_pct"
        )
    if not 0 < coefficient <= 100:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} coefficient_pct must be in (0, 100]"
        )
    if conditions.get("retroactive_violation") is not False:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} must declare retroactive_violation: false"
        )
    if conditions.get("blocks_proposal") is not False:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} must declare blocks_proposal: false"
        )
    cadence = conditions.get("recalculation_cadence")
    if not isinstance(cadence, str) or not cadence:
        raise DeploymentCapPolicyError(
            f"{DEPLOYMENT_CAP_RULE_KEY} requires a recalculation_cadence"
        )
    return DeploymentCapPolicy(
        coefficient_pct=Decimal(str(coefficient)),
        recalculation_cadence=cadence,
        retroactive_violation=False,
        blocks_proposal=False,
    )


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not number.is_finite():
        return None
    return number


def _text(value: Decimal) -> str:
    quantized = value.quantize(Decimal("1"))
    return format(quantized, "f")


def resolve_parking_balance_krw(
    manual_cash: Any,
) -> tuple[Decimal, str]:
    """Return the parking term and the provenance label that explains it.

    ``manual_cash`` is the shape ``get_available_capital_impl`` already builds:
    ``{"amount": float, "stale_warning": bool, ...}`` or ``None``. Anything
    unreadable, negative, or stale contributes 0 — the policy forbids
    estimating this term, and a stale balance is exactly the case where an
    estimate would be most tempting and least justified.
    """

    if not isinstance(manual_cash, dict):
        return Decimal("0"), PARKING_SOURCE_ABSENT
    amount = _decimal_or_none(manual_cash.get("amount"))
    if amount is None or amount <= 0:
        return Decimal("0"), PARKING_SOURCE_ABSENT
    if manual_cash.get("stale_warning") is True:
        return Decimal("0"), PARKING_SOURCE_STALE
    return amount, PARKING_SOURCE_MANUAL_CASH


def evaluate_deployment_cap(
    *,
    broker_orderable_total_krw: Any,
    parking_balance_krw: Any = 0,
    parking_balance_source: str = PARKING_SOURCE_ABSENT,
    planned_new_deployment_krw: Any = None,
    policy: Any | None = None,
) -> dict[str, Any]:
    """Compute the advisory cap and, when given, judge one NEW deployment.

    ``planned_new_deployment_krw`` is optional on purpose. Without it the
    result is the cap and its inputs — which is the whole advisory when the
    caller is reporting capital rather than proposing a deployment. With it,
    ``status`` compares that ONE new amount against the cap. Existing
    deployment is never an input, because it is never in breach.
    """

    stamp = policy_version_stamp()
    base: dict[str, Any] = {
        "policy_key": DEPLOYMENT_CAP_RULE_KEY,
        "policy_version": stamp["version"],
        "policy_content_hash": stamp["content_hash"],
        "currency": "KRW",
        "evaluated_at": "new_deployment_only",
        "retroactive_violation": False,
        "blocks_proposal": False,
    }

    try:
        contract = load_deployment_cap_policy(policy)
    except DeploymentCapPolicyError as exc:
        return {
            **base,
            "status": STATUS_UNAVAILABLE,
            "reason": "policy_unavailable",
            "detail": str(exc),
        }

    broker = _decimal_or_none(broker_orderable_total_krw)
    if broker is None or broker < 0:
        return {
            **base,
            "status": STATUS_UNAVAILABLE,
            "reason": "broker_orderable_total_unavailable",
            "coefficient_pct": _text(contract.coefficient_pct),
            "recalculation_cadence": contract.recalculation_cadence,
        }

    parking = _decimal_or_none(parking_balance_krw) or Decimal("0")
    if parking < 0:
        parking = Decimal("0")
        parking_balance_source = PARKING_SOURCE_ABSENT

    denominator = broker + parking
    cap = denominator * contract.coefficient_pct / Decimal("100")

    result: dict[str, Any] = {
        **base,
        "coefficient_pct": _text(contract.coefficient_pct),
        "recalculation_cadence": contract.recalculation_cadence,
        "broker_orderable_total_krw": _text(broker),
        "parking_balance_krw": _text(parking),
        "parking_balance_source": parking_balance_source,
        "denominator_krw": _text(denominator),
        "cap_krw": _text(cap),
        # The cap is a lower bound whenever the parking term was zeroed rather
        # than measured; saying so is the difference between a conservative
        # number and a wrong one.
        "cap_is_lower_bound": parking_balance_source != PARKING_SOURCE_MANUAL_CASH,
    }

    planned = _decimal_or_none(planned_new_deployment_krw)
    if planned is None:
        result["status"] = STATUS_UNAVAILABLE
        result["reason"] = "planned_new_deployment_not_supplied"
        return result

    if planned < 0:
        result["status"] = STATUS_UNAVAILABLE
        result["reason"] = "planned_new_deployment_negative"
        return result

    headroom = cap - planned
    result["planned_new_deployment_krw"] = _text(planned)
    result["headroom_krw"] = _text(headroom)
    result["status"] = STATUS_WITHIN if headroom >= 0 else STATUS_EXCEEDS
    if headroom < 0:
        result["excess_krw"] = _text(-headroom)
        result["warning"] = (
            f"신규 배치 {_text(planned)}원이 동적 한도 {_text(cap)}원"
            f"(주문가능 {_text(broker)} + 파킹 {_text(parking)} 의 "
            f"{_text(contract.coefficient_pct)}%)을 "
            f"{_text(-headroom)}원 초과합니다 — 경고이며 차단이 아닙니다."
        )
    return result


__all__ = [
    "DEPLOYMENT_CAP_RULE_KEY",
    "DEPLOYMENT_CAP_TIER_ID",
    "PARKING_SOURCE_ABSENT",
    "PARKING_SOURCE_MANUAL_CASH",
    "PARKING_SOURCE_STALE",
    "STATUS_EXCEEDS",
    "STATUS_UNAVAILABLE",
    "STATUS_WITHIN",
    "DeploymentCapPolicy",
    "DeploymentCapPolicyError",
    "evaluate_deployment_cap",
    "load_deployment_cap_policy",
    "resolve_parking_balance_krw",
]
