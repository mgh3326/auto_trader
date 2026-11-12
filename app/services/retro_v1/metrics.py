"""Pure, exact-decimal calculations for prospective trade retrospectives.

The functions here accept already selected evidence and never read or persist it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

MetricReason = Literal[
    "no_reference_price",
    "no_planned_quantity",
    "no_stop_recorded",
    "no_budget_cap",
    "stop_not_below_reference_price",
    "not_applicable",
    "missing_entry_price",
    "missing_entry_session",
    "no_risk_per_unit",
    "no_complete_session",
    "window_open",
    "missing_notional",
    "missing_currency",
    "rate_unavailable",
    "not_provided",
]


@dataclass(frozen=True, slots=True)
class RiskRung:
    """One planned entry rung and its selected reference-price provenance."""

    planned_quantity: Decimal | None
    reference_price: Decimal | None
    reference_price_kind: str | None
    reference_price_basis: Literal["recorded", "inference"] | None


@dataclass(frozen=True, slots=True)
class RiskAmount:
    amount: Decimal | None
    pct_of_notional: Decimal | None
    pct_of_budget: Decimal | None
    pct_of_budget_reason: MetricReason | None
    reference_price_kinds: tuple[str | None, ...]
    basis: Literal["recorded", "inference"] | None
    reason: MetricReason | None


@dataclass(frozen=True, slots=True)
class DailyBar:
    """A completed daily bar already selected from a single venue."""

    session: date
    high: Decimal
    low: Decimal


@dataclass(frozen=True, slots=True)
class Excursion:
    amount: Decimal | None
    pct: Decimal | None
    r: Decimal | None
    r_reason: MetricReason | None
    status: Literal["complete", "unknown", "pending"]
    reason: MetricReason | None


@dataclass(frozen=True, slots=True)
class MfeMae:
    mfe: Excursion
    mae: Excursion
    adjusted: None
    basis: Literal["inference"]


@dataclass(frozen=True, slots=True)
class RecordedCosts:
    fee: Decimal | None
    tax: Decimal | None
    currency: str | None


@dataclass(frozen=True, slots=True)
class EstimatedCosts:
    amount: Decimal
    rate_source: str
    policy_version: str | None
    policy_content_hash: str | None
    basis: Literal["inference"]


@dataclass(frozen=True, slots=True)
class Costs:
    recorded: RecordedCosts | None
    recorded_reason: MetricReason | None
    estimated: EstimatedCosts | None
    estimated_reason: MetricReason | None


def _decimal(value: Decimal | None, *, name: str) -> Decimal | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    return value


def calculate_risk_amount(
    *,
    side: Literal["buy", "sell"],
    stop_price: Decimal | None,
    stop_basis: Literal["recorded", "inference"] | None,
    rungs: Sequence[RiskRung],
    budget_cap: Decimal | None = None,
) -> RiskAmount:
    """Calculate long-entry risk, requiring complete rung inputs (no partial sums)."""
    stop = _decimal(stop_price, name="stop_price")
    budget = _decimal(budget_cap, name="budget_cap")
    kinds = tuple(rung.reference_price_kind for rung in rungs)
    if side == "sell":
        return RiskAmount(
            None, None, None, "not_applicable", kinds, None, "not_applicable"
        )
    if side != "buy":
        raise ValueError("side must be buy or sell")
    if budget is not None and budget <= 0:
        raise ValueError("budget_cap must be positive")
    if stop is None or stop_basis is None:
        return RiskAmount(None, None, None, None, kinds, None, "no_stop_recorded")
    if not rungs:
        return RiskAmount(None, None, None, None, kinds, None, "no_reference_price")
    quantities: list[Decimal] = []
    prices: list[Decimal] = []
    for rung in rungs:
        quantity = _decimal(rung.planned_quantity, name="planned_quantity")
        price = _decimal(rung.reference_price, name="reference_price")
        if quantity is None:
            return RiskAmount(
                None, None, None, None, kinds, None, "no_planned_quantity"
            )
        if price is None or rung.reference_price_kind is None:
            return RiskAmount(None, None, None, None, kinds, None, "no_reference_price")
        if quantity <= 0 or price <= 0:
            raise ValueError("planned quantities and reference prices must be positive")
        if price <= stop:
            return RiskAmount(
                None, None, None, None, kinds, None, "stop_not_below_reference_price"
            )
        quantities.append(quantity)
        prices.append(price)
    risk = sum(
        (price - stop) * quantity
        for price, quantity in zip(prices, quantities, strict=True)
    )
    notional = sum(
        price * quantity for price, quantity in zip(prices, quantities, strict=True)
    )
    basis: Literal["recorded", "inference"] = (
        "recorded"
        if stop_basis == "recorded"
        and all(rung.reference_price_basis == "recorded" for rung in rungs)
        else "inference"
    )
    pct_budget = risk / budget if budget is not None else None
    budget_reason = None if budget is not None else "no_budget_cap"
    return RiskAmount(
        risk, risk / notional, pct_budget, budget_reason, kinds, basis, None
    )


def _empty_excursion(
    status: Literal["unknown", "pending"], reason: MetricReason
) -> Excursion:
    return Excursion(None, None, None, None, status, reason)


def calculate_mfe_mae(
    *,
    entry_price: Decimal | None,
    entry_session: date | None,
    bars: Sequence[DailyBar] | None,
    exit_session: date | None = None,
    horizon_sessions: int | Literal["to_exit"] = "to_exit",
    risk_per_unit: Decimal | None = None,
) -> MfeMae:
    """Calculate daily excursions from completed sessions strictly inside the trade.

    Bars are assumed to be completed and selected from one venue by the caller.
    Entry and exit sessions are excluded because their extremes are not fully
    contained within the holding window.
    """
    entry = _decimal(entry_price, name="entry_price")
    risk = _decimal(risk_per_unit, name="risk_per_unit")
    if risk is not None and risk <= 0:
        raise ValueError("risk_per_unit must be positive")
    if entry is None:
        empty = _empty_excursion("unknown", "missing_entry_price")
        return MfeMae(empty, empty, None, "inference")
    if entry <= 0:
        raise ValueError("entry_price must be positive")
    if entry_session is None:
        empty = _empty_excursion("unknown", "missing_entry_session")
        return MfeMae(empty, empty, None, "inference")
    if horizon_sessions != "to_exit" and horizon_sessions <= 0:
        raise ValueError("horizon_sessions must be positive or to_exit")
    if horizon_sessions == "to_exit" and exit_session is None:
        empty = _empty_excursion("pending", "window_open")
        return MfeMae(empty, empty, None, "inference")

    contained = sorted(
        (
            bar
            for bar in (bars or ())
            if bar.session > entry_session
            and (exit_session is None or bar.session < exit_session)
        ),
        key=lambda bar: bar.session,
    )
    if len({bar.session for bar in contained}) != len(contained):
        raise ValueError("daily bars must have one bar per session")
    if horizon_sessions != "to_exit":
        contained = contained[:horizon_sessions]
    if not contained:
        reason: MetricReason = (
            "no_complete_session" if exit_session is not None else "window_open"
        )
        status: Literal["unknown", "pending"] = (
            "unknown" if exit_session is not None else "pending"
        )
        empty = _empty_excursion(status, reason)
        return MfeMae(empty, empty, None, "inference")
    if exit_session is None and horizon_sessions != "to_exit":
        # The caller must only pass bars known complete; fewer than the requested
        # count means the horizon has not closed yet.
        eligible = sorted(
            (bar for bar in (bars or ()) if bar.session > entry_session),
            key=lambda bar: bar.session,
        )
        if len(eligible) < horizon_sessions:
            empty = _empty_excursion("pending", "window_open")
            return MfeMae(empty, empty, None, "inference")

    for bar in contained:
        high = _decimal(bar.high, name="bar.high")
        low = _decimal(bar.low, name="bar.low")
        if high is None or low is None or low > high:
            raise ValueError("daily bar requires finite high and low with low <= high")
    mfe_amount = max(Decimal(0), max(bar.high for bar in contained) - entry)
    mae_amount = max(Decimal(0), entry - min(bar.low for bar in contained))

    def excursion(amount: Decimal) -> Excursion:
        return Excursion(
            amount=amount,
            pct=amount / entry,
            r=amount / risk if risk is not None and risk > 0 else None,
            r_reason=None if risk is not None else "no_risk_per_unit",
            status="complete",
            reason=None,
        )

    return MfeMae(excursion(mfe_amount), excursion(mae_amount), None, "inference")


def calculate_costs(
    *,
    fee: Decimal | None,
    tax: Decimal | None,
    currency: str | None,
    fee_basis: Literal["broker_reported", "not_provided"] | None,
    notional: Decimal | None,
    round_trip_cost_bps: Decimal | None,
    policy_version: str | None,
    policy_content_hash: str | None,
) -> Costs:
    """Keep reported costs separate from policy-based estimated round-trip cost."""
    fee_value = _decimal(fee, name="fee")
    tax_value = _decimal(tax, name="tax")
    amount = _decimal(notional, name="notional")
    bps = _decimal(round_trip_cost_bps, name="round_trip_cost_bps")

    recorded: RecordedCosts | None = None
    recorded_reason: MetricReason | None = None
    if fee_basis == "broker_reported" or fee_value is not None or tax_value is not None:
        recorded = RecordedCosts(fee_value, tax_value, currency)
        if currency is None:
            recorded_reason = "missing_currency"
    else:
        recorded_reason = "not_provided"

    estimated: EstimatedCosts | None = None
    estimated_reason: MetricReason | None = None
    if amount is None:
        estimated_reason = "missing_notional"
    elif amount < 0:
        raise ValueError("notional must not be negative")
    elif bps is None:
        estimated_reason = "rate_unavailable"
    elif bps < 0:
        raise ValueError("round_trip_cost_bps must not be negative")
    else:
        estimated = EstimatedCosts(
            amount=amount * bps / Decimal(10_000),
            rate_source="trading_policy.order_proposals.auto_approve.round_trip_cost_bps",
            policy_version=policy_version,
            policy_content_hash=policy_content_hash,
            basis="inference",
        )
    return Costs(recorded, recorded_reason, estimated, estimated_reason)
