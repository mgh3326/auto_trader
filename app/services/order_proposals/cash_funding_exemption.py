"""Closed cash-proxy funding exemption classifier (S177).

This module deliberately has no settings, database, broker, network, or
policy-loader dependency.  It only decides whether one already-shaped limit
sell may use the cash-funding exception; callers own fresh price and funding
shortfall measurement, and the durable cumulative cap remains outside this
pure classifier.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, DivisionByZero, InvalidOperation, Overflow
from typing import Any

from app.services.order_proposals.parking_allowlist import parking_scope

CASH_FUNDING_EXIT_INTENT = "cash_funding"

# Closed operator constant.  It is intentionally not a setting, environment
# variable, policy value, or caller input.
TRANCHE_SLACK_UNITS = Decimal("1")

# Closed input/resource limits.  A funding target is a compact native-currency
# amount, never an unbounded decimal payload.  Keeping both the coefficient and
# adjusted exponent bounded also guarantees that audit details cannot expand a
# scientific-notation input into an unbounded fixed-point string.
MAX_FUNDING_REQUIRED_DIGITS = 18
MAX_FUNDING_REQUIRED_ADJUSTED = 18
MAX_FUNDING_DECIMAL_TEXT_LENGTH = 64
_UNREPRESENTABLE_DECIMAL_TEXT = "unrepresentable"


@dataclass(frozen=True)
class FundingTarget:
    """The planned buy whose native-currency shortfall is being funded."""

    market: str
    required: Decimal
    plan_ref: str


@dataclass(frozen=True)
class CashFundingVerdict:
    """One closed-vocabulary result from :func:`resolve_cash_funding_exemption`."""

    exempt: bool
    reason: str
    max_quantity: Decimal | None
    scope_currency: str | None
    details: dict[str, str]


# These are deliberately a finite, public audit vocabulary.  ``exempt`` is
# the one successful verdict and is therefore not a member of this reject set.
CASH_FUNDING_REJECT_REASONS: frozenset[str] = frozenset(
    {
        "not_cash_funding",
        "side_not_sell",
        "order_type_not_limit",
        "symbol_not_cash_proxy",
        "funding_target_missing",
        "funding_currency_mismatch",
        "funding_required_invalid",
        "funding_plan_ref_missing",
        "shortfall_unmeasured",
        "no_measured_shortfall",
        "required_exceeds_measured_shortfall",
        "current_price_unavailable",
        "quantity_invalid",
        "quantity_exceeds_funding_need",
    }
)

_FUNDING_MARKET_CURRENCY: dict[str, str] = {
    "equity_kr": "KRW",
    "kr": "KRW",
    "equity_us": "USD",
    "us": "USD",
}


def _funding_decimal_within_bounds(value: Decimal) -> bool:
    """Return whether a finite funding amount is safe to retain or render."""
    if not value.is_finite():
        return False
    try:
        return (
            len(value.as_tuple().digits) <= MAX_FUNDING_REQUIRED_DIGITS
            and abs(value.adjusted()) <= MAX_FUNDING_REQUIRED_ADJUSTED
        )
    except (InvalidOperation, Overflow, ValueError):
        return False


def _decimal_from_payload(value: Any) -> Decimal | None:
    """Parse only exact bounded numeric forms; floats are never accepted."""
    if type(value) is Decimal:
        parsed = value
    elif type(value) is int:
        parsed = Decimal(value)
    elif type(value) is not str or not value or value.strip() != value:
        return None
    else:
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return None

    # Preserve the parsed target shape so the resolver reports the dedicated
    # `funding_required_invalid` reason rather than conflating hostile numeric
    # input with a missing target.  Decimal NaN is never usable as an amount.
    if parsed.is_finite() and not _funding_decimal_within_bounds(parsed):
        return Decimal("NaN")
    return parsed


def _positive_finite_decimal(value: Any) -> Decimal | None:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        return None
    return value


def _decimal_text(value: Decimal) -> str:
    """Return a bounded audit representation without propagating decimal errors."""
    if not _funding_decimal_within_bounds(value):
        return _UNREPRESENTABLE_DECIMAL_TEXT
    try:
        normalized = format(value.normalize(), "f")
    except (InvalidOperation, Overflow, ValueError):
        return _UNREPRESENTABLE_DECIMAL_TEXT
    rendered = normalized.rstrip("0").rstrip(".") if "." in normalized else normalized
    return (
        rendered
        if len(rendered) <= MAX_FUNDING_DECIMAL_TEXT_LENGTH
        else _UNREPRESENTABLE_DECIMAL_TEXT
    )


def _funding_currency(market: Any) -> str | None:
    return _FUNDING_MARKET_CURRENCY.get(market) if type(market) is str else None


def _verdict(
    *,
    exempt: bool,
    reason: str,
    max_quantity: Decimal | None = None,
    scope_currency: str | None = None,
    details: dict[str, str] | None = None,
) -> CashFundingVerdict:
    # Keep accidental dynamic error text out of the durable/audit surface.
    if exempt:
        assert reason == "exempt"
    else:
        assert reason in CASH_FUNDING_REJECT_REASONS
    return CashFundingVerdict(
        exempt=exempt,
        reason=reason,
        max_quantity=max_quantity,
        scope_currency=scope_currency,
        details=dict(details or {}),
    )


def parse_funding_target(payload: Any) -> FundingTarget | None:
    """Parse the exact persisted funding-target shape without coercing floats.

    Empty/invalid `plan_ref` and non-positive/non-finite `required` are retained
    for the resolver to classify with their dedicated closed reasons.  A shape
    that cannot be parsed at all returns ``None`` and is treated as missing.
    """
    if type(payload) is not dict:
        return None
    market = payload.get("market")
    plan_ref = payload.get("plan_ref")
    required = _decimal_from_payload(payload.get("required"))
    if type(market) is not str or type(plan_ref) is not str or required is None:
        return None
    return FundingTarget(market=market, required=required, plan_ref=plan_ref)


def resolve_cash_funding_exemption(
    *,
    exit_intent: Any,
    symbol: Any,
    account_mode: Any,
    market: Any,
    side: Any,
    order_type: Any,
    funding_target: FundingTarget | None,
    quantity: Decimal | None,
    current_price: Decimal | None,
    measured_shortfall: Decimal | None,
) -> CashFundingVerdict:
    """Fail-closed cash-funding decision in its policy-mandated order.

    The classifier never reads a balance, quote, policy, or ledger.  It accepts
    only values that its callers have already measured and makes no attempt to
    repair malformed input.  In particular, the daily cumulative cap is an
    account/day state boundary and intentionally belongs to auto-approval.
    """
    if type(exit_intent) is not str or exit_intent != CASH_FUNDING_EXIT_INTENT:
        return _verdict(exempt=False, reason="not_cash_funding")
    if type(side) is not str or side != "sell":
        return _verdict(exempt=False, reason="side_not_sell")
    if type(order_type) is not str or order_type != "limit":
        return _verdict(exempt=False, reason="order_type_not_limit")

    scope = parking_scope(symbol=symbol, account_mode=account_mode, market=market)
    if scope is None:
        return _verdict(exempt=False, reason="symbol_not_cash_proxy")
    scope_currency = scope.currency

    if type(funding_target) is not FundingTarget:
        return _verdict(
            exempt=False,
            reason="funding_target_missing",
            scope_currency=scope_currency,
        )
    if _funding_currency(funding_target.market) != scope_currency:
        return _verdict(
            exempt=False,
            reason="funding_currency_mismatch",
            scope_currency=scope_currency,
        )

    required = _positive_finite_decimal(funding_target.required)
    if required is None:
        return _verdict(
            exempt=False,
            reason="funding_required_invalid",
            scope_currency=scope_currency,
        )
    if type(funding_target.plan_ref) is not str or not funding_target.plan_ref.strip():
        return _verdict(
            exempt=False,
            reason="funding_plan_ref_missing",
            scope_currency=scope_currency,
        )

    if (
        measured_shortfall is None
        or type(measured_shortfall) is not Decimal
        or not measured_shortfall.is_finite()
    ):
        return _verdict(
            exempt=False,
            reason="shortfall_unmeasured",
            scope_currency=scope_currency,
        )
    if measured_shortfall <= 0:
        return _verdict(
            exempt=False,
            reason="no_measured_shortfall",
            scope_currency=scope_currency,
        )
    if required > measured_shortfall:
        return _verdict(
            exempt=False,
            reason="required_exceeds_measured_shortfall",
            scope_currency=scope_currency,
            details={
                "required": _decimal_text(required),
                "measured_shortfall": _decimal_text(measured_shortfall),
            },
        )

    resolved_price = _positive_finite_decimal(current_price)
    if resolved_price is None:
        return _verdict(
            exempt=False,
            reason="current_price_unavailable",
            scope_currency=scope_currency,
        )
    resolved_quantity = _positive_finite_decimal(quantity)
    if resolved_quantity is None:
        return _verdict(
            exempt=False,
            reason="quantity_invalid",
            scope_currency=scope_currency,
        )

    try:
        max_quantity = (required / resolved_price).quantize(
            Decimal("1"), rounding=ROUND_CEILING
        ) + TRANCHE_SLACK_UNITS
    except (DivisionByZero, InvalidOperation):
        return _verdict(
            exempt=False,
            reason="current_price_unavailable",
            scope_currency=scope_currency,
        )
    if resolved_quantity > max_quantity:
        return _verdict(
            exempt=False,
            reason="quantity_exceeds_funding_need",
            max_quantity=max_quantity,
            scope_currency=scope_currency,
            details={
                "required": _decimal_text(required),
                "current_price": _decimal_text(resolved_price),
                "max_quantity": _decimal_text(max_quantity),
                "quantity": _decimal_text(resolved_quantity),
            },
        )
    return _verdict(
        exempt=True,
        reason="exempt",
        max_quantity=max_quantity,
        scope_currency=scope_currency,
        details={
            "required": _decimal_text(required),
            "measured_shortfall": _decimal_text(measured_shortfall),
            "current_price": _decimal_text(resolved_price),
            "max_quantity": _decimal_text(max_quantity),
        },
    )


__all__ = [
    "CASH_FUNDING_EXIT_INTENT",
    "CASH_FUNDING_REJECT_REASONS",
    "MAX_FUNDING_DECIMAL_TEXT_LENGTH",
    "TRANCHE_SLACK_UNITS",
    "CashFundingVerdict",
    "FundingTarget",
    "parse_funding_target",
    "resolve_cash_funding_exemption",
]
