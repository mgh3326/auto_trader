"""Replay the existing pure loss-cut rules on an immutable plan input."""

from datetime import date
from decimal import Decimal
from typing import Any, cast

from app.schemas.trading_policy import MechanicalLossCutMarket
from app.services.daily_plan.bundle import decimal_string
from app.services.daily_plan.validator import parse_positive_decimal, risk_input_gap
from app.services.mechanical_loss_cut.rules import (
    Bar,
    ExitPlan,
    HeldLot,
    LotKey,
    PriorDecision,
    Skip,
    StopBasis,
    evaluate_lot,
)

UNAVAILABLE_SKIPS = frozenset(
    {
        "no_exit_plan",
        "position_unreadable",
        "price_missing",
        "price_stale",
        "halted_suspect",
        "open_sell_unknown",
        "protected_unknown",
    }
)
EVALUATED_SKIPS = frozenset(
    {
        "not_underwater",
        "above_planned_stop",
        "floor_not_reached",
        "rising",
        "not_n_day_low",
        "trailing_drop_not_reached",
        "covered_by_open_sell",
        "below_one_unit",
        "already_cut_this_run",
        "cooldown",
    }
)
NOT_APPLICABLE_SKIPS = frozenset({"parking_instrument"})


def replay_risk(
    evidence: dict[str, Any],
    params: MechanicalLossCutMarket,
    *,
    symbol: str,
) -> dict[str, Any]:
    raw = evidence.get("risk_inputs")
    if raw is None:
        return {
            "result": "skip",
            "skip_reason": "position_unreadable",
            "status": "unknown",
        }
    if gap := risk_input_gap(raw):
        default_skip, reason = gap
        return {
            "result": "skip",
            "skip_reason": reason if reason in UNAVAILABLE_SKIPS else default_skip,
            "status": "unknown",
        }
    key = LotKey(evidence["account_mode"], "equity_kr", symbol)
    lot = HeldLot(
        key,
        parse_positive_decimal(raw["quantity"]),
        parse_positive_decimal(raw["average_cost"]),
        "KRW",
    )
    stop = raw.get("plan")
    plan = None
    if stop is not None:
        if stop["stop_basis"] not in ("support", "pct_floor", "volatility"):
            raise ValueError("invalid_stop_basis")
        plan = ExitPlan(
            key=key,
            event="entry",
            stop_price=parse_positive_decimal(stop["stop_price"]),
            stop_basis=cast(StopBasis, stop["stop_basis"]),
            stop_reason=None,
            reference_price=lot.average_cost,
            average_cost=lot.average_cost,
            quantity=lot.quantity,
            add_ladder=(),
            max_adds=0,
            adds_used=0,
            add_verdict=None,
            backfilled=stop["backfilled"],
            id=stop["id"],
            version=stop["version"],
        )
    bars = [
        Bar(
            session=date.fromisoformat(bar["session"]),
            high=parse_positive_decimal(bar["high"]),
            low=parse_positive_decimal(bar["low"]),
            close=parse_positive_decimal(bar["close"]),
            volume=_known_quantity(bar.get("volume")),
            open=parse_positive_decimal(bar["open"])
            if bar.get("open") is not None
            else None,
        )
        for bar in raw["bars"]
    ]
    if any(a.session >= b.session for a, b in zip(bars, bars[1:], strict=False)):
        raise ValueError("invalid_risk_bar_order")
    prior = raw.get("prior_decision")
    decision = evaluate_lot(
        lot=lot,
        plan=plan,
        bars=bars,
        params=params,
        price_fresh=raw["price_fresh"] is True,
        open_sell_quantity=_known_quantity(raw.get("open_sell_quantity")),
        protected_quantity=_known_quantity(raw.get("protected_quantity")),
        prior_decision=PriorDecision(
            date.fromisoformat(prior["session"]),
            prior["plan_version"],
            parse_positive_decimal(prior["stop_price"]),
            prior["cuts_on_this_stop"],
        )
        if prior
        else None,
    )
    if isinstance(decision, Skip):
        if decision.reason in UNAVAILABLE_SKIPS:
            status = "unknown"
        elif decision.reason in NOT_APPLICABLE_SKIPS:
            status = "not_applicable"
        else:
            status = "evaluated"
        return {"result": "skip", "skip_reason": decision.reason, "status": status}
    return {
        "result": "cut",
        "status": "evaluated",
        "trigger_key": decision.trigger_key,
        "quantity": decimal_string(decision.intended_quantity),
        "close": decimal_string(decision.close),
        "stop": decimal_string(decision.stop_price),
        "unrealized_pct": decimal_string(decision.unrealized_pct),
        "plan_version": decision.plan_version,
        "thresholds_confirmed": params.thresholds_confirmed,
    }


def _known_quantity(raw: dict | None) -> Decimal | None:
    if raw is None or "known" not in raw:
        return None
    value = raw["known"]
    return Decimal(0) if value == "0" else parse_positive_decimal(value)
