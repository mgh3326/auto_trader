"""Pure invariant functions of the mandate model (hk 3795 T1, §3.2/§3.3/§3.8).

No database, clock, broker or order module: every input is a value the caller
read. The store calls these at registration; later tickets call the same
functions at dispatch and arming, so one rule has one implementation.

* I2  ``symbol_allowed``
* I3  ``sleeve_budget_check``, ``basket_budget_check``
* I4  ``sector_cap_check`` (plan-amount basis, §3.8)
* I5  ``tranche_trigger_met`` (division-free, §3.2 "회차 기준값")
* I6  ``min_gap_met``
* I7  ``tranche_order_ok``
* I15 ``account_binding_ok`` (registration side)
* I17 ``is_parking_symbol``
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState
from app.services.order_proposals.parking_allowlist import (
    PARKING_ALLOWLIST_SCOPES,
    canonical_exposure_symbol,
)

ZERO = Decimal("0")
HUNDRED = Decimal("100")
_CENT = Decimal("0.0001")  # NUMERIC(20, 4)

#: Every symbol a parking scope names, in any account/market (I17). Over-inclusion
#: only refuses a plan; it never lets one through.
PARKING_SYMBOLS: frozenset[str] = frozenset(
    scope.symbol for scope in PARKING_ALLOWLIST_SCOPES
)


class InvariantViolation(ValueError):
    """A registration the invariants refuse; ``code`` is a ``MandateRefusal``."""

    def __init__(self, code: v.MandateRefusal, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


# --- I17 / I2 ---------------------------------------------------------------
def is_parking_symbol(symbol: str) -> bool:
    canonical = canonical_exposure_symbol(symbol)
    # An unreadable symbol is refused too (fail closed).
    return canonical is None or canonical.upper() in PARKING_SYMBOLS


def symbol_allowed(symbol: str, allowed_symbols: Iterable[str]) -> bool:
    """I2: the symbol is on the terms' ``allowed_symbols`` (both normalized)."""
    return symbol in set(allowed_symbols)


# --- tranche amounts (§3.2 table row "amount") ------------------------------
def parse_weights(weights: Sequence[object]) -> tuple[Decimal, ...]:
    try:
        parsed = tuple(Decimal(str(w)) for w in weights)
    except InvalidOperation as exc:
        raise InvariantViolation(
            v.MandateRefusal.INVALID_FIELDS, "tranche weights must be numbers"
        ) from exc
    if (
        len(parsed) != v.MAX_TRANCHES
        or any(not w.is_finite() or w <= 0 for w in parsed)
        or sum(parsed) != Decimal(1)
    ):
        raise InvariantViolation(
            v.MandateRefusal.INVALID_FIELDS,
            f"tranche weights must be {v.MAX_TRANCHES} positive numbers summing to 1",
        )
    return parsed


def tranche_amounts(
    planned: Decimal, adopted: Decimal, weights: Sequence[object]
) -> tuple[Decimal, ...]:
    """``(planned - adopted) x weight`` per tranche, exact at the column scale.

    A weight that does not divide the new amount exactly at 4 decimals, or
    amounts that do not add back to ``planned - adopted``, are refused: the
    tranche rows must account for every won of the plan, never round it away.
    """
    if not (planned > 0 and 0 <= adopted <= planned):
        raise InvariantViolation(
            v.MandateRefusal.INVALID_FIELDS,
            "need 0 <= adopted_amount <= planned_amount",
        )
    new_amount = planned - adopted
    amounts = tuple(new_amount * w for w in parse_weights(weights))
    if any(a != a.quantize(_CENT) for a in amounts) or sum(amounts) != new_amount:
        raise InvariantViolation(
            v.MandateRefusal.INVALID_FIELDS,
            "tranche amounts do not add up exactly to planned_amount - adopted_amount",
        )
    return tuple(a.quantize(_CENT) for a in amounts)


def check_one_share(amounts: Sequence[Decimal], one_share_price: Decimal) -> None:
    """Every tranche must afford one share at registration (§3.2 row 완료)."""
    if not one_share_price.is_finite() or one_share_price <= 0:
        raise InvariantViolation(
            v.MandateRefusal.TRANCHE_BELOW_ONE_SHARE, "no usable one-share price"
        )
    if any(a < one_share_price for a in amounts):
        raise InvariantViolation(
            v.MandateRefusal.TRANCHE_BELOW_ONE_SHARE,
            "a tranche amount is below the price of one share",
        )


# --- I3 ---------------------------------------------------------------------
@dataclass(frozen=True)
class PlanAmounts:
    """What the budget and sector rules read from one unreleased plan."""

    planned: Decimal
    adopted: Decimal
    sector_key: str = ""

    @property
    def new_amount(self) -> Decimal:
        return self.planned - self.adopted


def sleeve_budget_check(
    new: PlanAmounts, existing: Iterable[PlanAmounts], budget_cap: Decimal
) -> None:
    """I3 (a): unreleased new-buy amounts of the sleeve plus this plan <= ``budget_cap``."""
    total = new.new_amount + sum((p.new_amount for p in existing), ZERO)
    if total > budget_cap:
        raise InvariantViolation(
            v.MandateRefusal.BUDGET_EXCEEDED,
            f"new-buy amounts {total} exceed the sleeve budget {budget_cap}",
        )


def basket_budget_check(
    new: PlanAmounts,
    existing: Iterable[PlanAmounts],
    sleeve_budget_caps: Iterable[Decimal],
) -> None:
    """I3 (b): follows from (a) in every sleeve; kept as a value-level check."""
    total = new.new_amount + sum((p.new_amount for p in existing), ZERO)
    if total > sum(sleeve_budget_caps, ZERO):
        raise InvariantViolation(
            v.MandateRefusal.BUDGET_EXCEEDED, "basket new-buy amounts exceed its budget"
        )


# --- I4 ---------------------------------------------------------------------
def sector_cap_check(
    new: PlanAmounts,
    existing: Iterable[PlanAmounts],
    constituent_budget_caps: Iterable[Decimal],
    sector_cap_pct: Decimal,
) -> None:
    """I4 on the plan-amount basis of §3.8.

    numerator   = P of the unreleased plans in the sector, plus this plan's P
    denominator = the basket's constituent sleeve budgets + A of every
                  unreleased plan of the basket, this one included
    The sector must satisfy ``numerator x 100 <= denominator x sector_cap_pct``.
    """
    plans = [*existing, new]
    numerator = sum((p.planned for p in plans if p.sector_key == new.sector_key), ZERO)
    denominator = sum(constituent_budget_caps, ZERO) + sum(
        (p.adopted for p in plans), ZERO
    )
    if numerator * HUNDRED > denominator * sector_cap_pct:
        raise InvariantViolation(
            v.MandateRefusal.SECTOR_CAP,
            f"sector {new.sector_key!r}: {numerator} of {denominator} exceeds "
            f"{sector_cap_pct}%",
        )


# --- I5 / I6 / I7 -------------------------------------------------------------
def tranche_trigger_met(
    current_price: Decimal,
    first_fill_amount: Decimal,
    first_fill_quantity: Decimal,
    trigger_pct_from_first: Decimal,
) -> bool:
    """I5, without a division: ``price x qty x 100 <= amount x (100 + trigger)``.

    ``first_fill_*`` are the sums recorded once when the first tranche completed.
    The average price they imply is for display only and never enters a decision.
    The boundary is inclusive.
    """
    if not (
        current_price.is_finite()
        and current_price > 0
        and first_fill_quantity > 0
        and first_fill_amount > 0
    ):
        return False
    return current_price * first_fill_quantity * HUNDRED <= first_fill_amount * (
        HUNDRED + trigger_pct_from_first
    )


def min_gap_met(
    settled_on: date | None,
    on: date,
    sessions: Sequence[date],
    min_gap_sessions: int,
) -> bool:
    """I6: at least ``min_gap_sessions`` sessions after the settle day, through ``on``.

    ``sessions`` are market-calendar trading days (the caller reads them). A day
    that is not itself a session counts the sessions before it. No settle day,
    no gap: the answer is False.
    """
    if settled_on is None:
        return False
    elapsed = sum(1 for s in sessions if settled_on < s <= on)
    return elapsed >= min_gap_sessions


def tranche_order_ok(state: PlanState, index: int) -> bool:
    """I7: tranche k arms only after k-1 completed."""
    return index == 1 or state.tranche(index - 1).completed


# --- I15 (registration side) ---------------------------------------------------
def scope_size(realm: str) -> int:
    """A live plan's holdings scope is both real accounts, a mock plan's its one."""
    return 2 if realm == "live" else 1


def account_binding_ok(
    *,
    terms_account_ref: str,
    plan_account_ref: str,
    scope_account_refs: Sequence[str],
    realm: str,
) -> bool:
    """The plan's order account is the clause account and one of its scope accounts.

    The scope is exactly the realm's holdings range (§3.7): two distinct
    accounts for a live plan, one for a mock plan. A shorter scope would leave
    an account out of the adopted amount and of the zero-holdings proof.
    """
    refs = tuple(scope_account_refs)
    return (
        bool(plan_account_ref)
        and plan_account_ref == terms_account_ref
        and plan_account_ref in refs
        and len(refs) == scope_size(realm)
        and len(set(refs)) == len(refs)
        and all(refs)
    )
