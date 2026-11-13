"""Pure decision for the hk 4178 KIS US operator inference."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.services.execution_ledger.kis_lots import us_trading_day_window

DECISION_REF = "decision/2026-11-12/at-b02-reconcile-and-3792#2"
RULE_VERSION = "kis_us_day_order_inference_v1"
MARKER = "expired_inference:kis_us_day_order_no_broker_original"
DAY_REASON = (
    "order_kind limit/market; KIS US sends ORD_DVSN 00/01; ROB-671 us_day_order"
)
DEADLINE_REASON = "deadline=us_trading_day_end_20_00_et (upper bound for a proven DAY order, not a broker expiry contract)"
CONDITIONS = (
    "open_owned",
    "day_order",
    "deadline_passed",
    "inquiry_window_passed",
    "no_order_fills",
    "no_symbol_fills",
    "reconcile_coverage",
)


@dataclass(frozen=True)
class Facts:
    ledger_id: int
    broker: str
    account_scope: str
    market: str
    status: str
    lifecycle_state: str
    order_kind: str | None
    accepted_at: datetime | None
    filled_qty: Decimal | None
    identity_valid: bool
    order_fills: int | None
    symbol_fills: int | None
    covering_run: bool | None


def deadline_for(accepted_at: datetime | None) -> datetime | None:
    if accepted_at is None or accepted_at.tzinfo is None:
        return None
    try:
        return us_trading_day_window(accepted_at)[1]
    except (ValueError, OverflowError, OSError):
        return None


def classify(facts: Facts, now: datetime) -> dict:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    deadline = deadline_for(facts.accepted_at)
    try:
        inquiry_end = deadline + timedelta(days=90) if deadline is not None else None
    except OverflowError:
        inquiry_end = None
    results = {
        "open_owned": facts.broker == "kis"
        and facts.account_scope == "kis_live"
        and facts.market == "us"
        and facts.status in ("accepted", "pending")
        and facts.lifecycle_state in ("accepted", "pending")
        and (facts.filled_qty is None or facts.filled_qty == 0),
        "day_order": facts.order_kind in ("limit", "market"),
        "deadline_passed": deadline is not None and now > deadline,
        "inquiry_window_passed": inquiry_end is not None and now > inquiry_end,
        "no_order_fills": facts.identity_valid and facts.order_fills == 0,
        "no_symbol_fills": facts.symbol_fills == 0,
        "reconcile_coverage": facts.covering_run is True,
    }
    return {
        "ledger_id": facts.ledger_id,
        "eligible": all(results.values()),
        "conditions": results,
        "day_reason": DAY_REASON,
        "deadline_reason": DEADLINE_REASON,
        "accepted_at": facts.accepted_at.isoformat() if facts.accepted_at else None,
        "deadline": deadline.isoformat() if deadline else None,
        "order_fills": facts.order_fills,
        "symbol_fills": facts.symbol_fills,
        "covering_run": facts.covering_run,
    }
