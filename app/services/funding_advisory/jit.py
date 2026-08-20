"""Just-in-time funding disposition — derived label, never a cash input.

Operator principle (§107): do not pre-fund.  Deposit only the cash a candidate
actually needs, at the moment the order is confirmed.  A buy candidate that
cleared every non-funding gate but is short on broker cash therefore must not
be rejected; it stays open with the deposit amount attached.

This module is pure (stdlib + Decimal): it consumes values that were already
computed from broker-authoritative inputs and returns a disclosure block.

Two invariants live here and are covered by tests:

* The deposit amount ``X`` is the **candidate shortfall**, never the declared
  external-cash total.  A larger declaration cannot shrink or grow ``X``.
* Operator-declared cash is display evidence only.  Nothing returned by this
  module is an available / required / shortfall / sizing / cap / eligibility
  input, and no caller may route it back into one.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from app.schemas.funding_advisory import canonical_decimal

DEFERRED_WITH_CONDITION = "deferred_with_condition"
FUNDABLE_NOW = "fundable_now"

CONDITION_KIND = "operator_deposit_to_target_account"
DEPOSIT_AMOUNT_BASIS = "candidate_shortfall"
SATISFIED_BY = "target_broker_buying_power_reobservation"

NEXT_STEP_DEPOSIT = "operator_deposit_then_reevaluate"
NEXT_STEP_PROPOSAL = "existing_proposal_creation_and_approval_path"

DeclaredCover = Literal["sufficient", "partial", "none"]


def declared_cover(*, shortfall: Decimal, declared_total: Decimal) -> DeclaredCover:
    """Classify how far declared external cash would go — display only."""

    if declared_total <= 0 or shortfall <= 0:
        return "none"
    if declared_total >= shortfall:
        return "sufficient"
    return "partial"


def build_jit_funding(
    *,
    shortfall: Decimal,
    operational_gap: Decimal,
    currency: str,
    declared_total: Decimal,
) -> dict[str, Any]:
    """Return the derived JIT disposition for one evaluated candidate.

    ``declared_total`` is the fresh, same-currency operator declaration sum.
    It is echoed for disclosure and classified into ``declared_cover``; it is
    never added to, subtracted from, or otherwise mixed into ``deposit_amount``.
    """

    common: dict[str, Any] = {
        "rejected_for_insufficient_cash": False,
        "creates_proposal": False,
        "executes_money_movement": False,
        "declared_cash_counted_toward_buying_power": False,
        "declared_cash_is_display_evidence_only": True,
    }
    if shortfall <= 0:
        return {
            "disposition": FUNDABLE_NOW,
            "condition": None,
            "next_step": NEXT_STEP_PROPOSAL,
            **common,
        }
    return {
        "disposition": DEFERRED_WITH_CONDITION,
        "condition": {
            "kind": CONDITION_KIND,
            "deposit_amount": canonical_decimal(shortfall),
            "deposit_amount_basis": DEPOSIT_AMOUNT_BASIS,
            "currency": currency,
            "operational_gap_amount": canonical_decimal(operational_gap),
            "declared_cover": declared_cover(
                shortfall=shortfall, declared_total=declared_total
            ),
            "declared_total_disclosure_only": canonical_decimal(declared_total),
            "satisfied_by": SATISFIED_BY,
        },
        "next_step": NEXT_STEP_DEPOSIT,
        **common,
    }


__all__ = [
    "CONDITION_KIND",
    "DEFERRED_WITH_CONDITION",
    "DEPOSIT_AMOUNT_BASIS",
    "FUNDABLE_NOW",
    "NEXT_STEP_DEPOSIT",
    "NEXT_STEP_PROPOSAL",
    "SATISFIED_BY",
    "build_jit_funding",
    "declared_cover",
]
