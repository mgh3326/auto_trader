"""Conservative row-local eligibility for the operator's KIS mock DAY decision.

The native path is order_execution._execute_and_record ->
kis_mock_ledger._record_kis_mock_order -> _save_kis_mock_order_ledger. The
domestic order-cash client maps a positive limit price to ORD_DVSN 00 and a
zero market price to 01. Its accepted response carries odno/ord_tmd/rt_cd.
Synthetic mock_scalping_exec rows also use the save helper, but lack that
positive response and carry scalping_role. No broker client is used here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.review import KISMockOrderLedger
from app.schemas.execution_contracts import TERMINAL_LIFECYCLE_STATES
from app.services.kis_mock_attribution import MissingAttribution, validate_strategy
from app.services.market_events.session_calendar import trading_session_status

RULE_VERSION = "kis_mock_legacy_day_expiry_q46_v1"
MAX_LEDGER_IDS = 50
MAX_DECISION_REF = 120
MAX_STRATEGY = 100
MAX_CALENDAR_DAYS = 366
_KST = timezone(timedelta(hours=9))
_ORDER_NO = re.compile(r"[0-9]{1,20}\Z")
_SYMBOL = re.compile(r"[0-9]{6}\Z")
_TIME = re.compile(r"[0-9]{6}\Z")
_NO_FILL_REASONS = frozenset({"pending_unconfirmed"})
_NATIVE_RESPONSE_KEYS = frozenset({"odno", "ord_tmd", "msg", "rt_cd", "msg_cd"})
CalendarStatus = Callable[[str, date], str]


def validate_request(
    ledger_ids: list[int], operator_decision_ref: str, expected_strategy: str
) -> str | None:
    if type(ledger_ids) is not list or not 1 <= len(ledger_ids) <= MAX_LEDGER_IDS:
        return "ledger_ids_invalid"
    if any(
        type(item) is not int or item <= 0 or item > 2**63 - 1 for item in ledger_ids
    ):
        return "ledger_ids_invalid"
    if len(set(ledger_ids)) != len(ledger_ids):
        return "ledger_ids_duplicate"
    if (
        type(operator_decision_ref) is not str
        or not 1 <= len(operator_decision_ref.strip()) <= MAX_DECISION_REF
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in operator_decision_ref)
    ):
        return "operator_decision_ref_invalid"
    if (
        type(expected_strategy) is not str
        or not 1 <= len(expected_strategy.strip()) <= MAX_STRATEGY
        or expected_strategy != expected_strategy.strip()
        or any(ord(ch) < 32 or ord(ch) == 127 for ch in expected_strategy)
    ):
        return "expected_strategy_invalid"
    try:
        validate_strategy(expected_strategy)
    except MissingAttribution:
        return "expected_strategy_invalid"
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _session_age(
    order_date: date, today: date, calendar: CalendarStatus
) -> tuple[int | None, str | None]:
    if order_date >= today:
        return None, "trade_date_not_past"
    days = (today - order_date).days
    if days > MAX_CALENDAR_DAYS:
        return None, "calendar_range_exceeded"
    age = 0
    for offset in range(days):
        day = order_date + timedelta(days=offset)
        try:
            status = calendar("kr", day)
        except Exception:  # noqa: BLE001 - an uncertain calendar must refuse
            return None, "calendar_unknown"
        if status == "unknown" or status not in {"open", "closed"}:
            return None, "calendar_unknown"
        if offset == 0:
            if status != "open":
                return None, "trade_date_not_session"
        elif status == "open":
            age += 1
    return age, None


def classify_row(
    row: KISMockOrderLedger,
    *,
    expected_strategy: str,
    today: date,
    min_sessions: int,
    calendar: CalendarStatus = trading_session_status,
) -> tuple[str, dict[str, Any]]:
    """Return a fixed refusal code or eligible, plus bounded local evidence."""
    evidence: dict[str, Any] = {
        "scope": "row_local_and_xkrx_no_broker_read",
        "rule_version": RULE_VERSION,
        "age_sessions": None,
        "ord_dvsn": None,
        "zero_fill_recorded": False,
    }
    state = row.lifecycle_state
    if state in TERMINAL_LIFECYCLE_STATES:
        return "already_terminal", evidence
    if state not in {"accepted", "pending"}:
        return "state_not_unfilled", evidence
    if row.account_mode != "kis_mock" or row.broker != "kis":
        return "foreign_account_or_broker", evidence
    if row.instrument_type != "equity_kr" or row.currency != "KRW":
        return "not_kr_cash_equity", evidence
    if row.strategy != expected_strategy:
        return "strategy_mismatch", evidence
    try:
        validate_strategy(row.strategy)
    except MissingAttribution:
        return "strategy_mismatch", evidence
    if (
        row.status != "accepted"
        or row.scalping_role is not None
        or not isinstance(row.correlation_id, str)
        or not row.correlation_id.strip()
        or row.mirror_cohort is not None
        or row.mirror_source_bucket is not None
        or row.report_item_uuid is not None
    ):
        return "native_source_unproven", evidence
    if not isinstance(row.symbol, str) or _SYMBOL.fullmatch(row.symbol) is None:
        return "order_identity_invalid", evidence
    if row.side not in {"buy", "sell"}:
        return "order_identity_invalid", evidence
    order_no = row.order_no
    if not isinstance(order_no, str) or _ORDER_NO.fullmatch(order_no) is None:
        return "order_identity_invalid", evidence
    raw = row.raw_response
    if (
        type(raw) is not dict
        or set(raw) != _NATIVE_RESPONSE_KEYS
        or row.response_code != "0"
        or raw.get("rt_cd") != "0"
        or type(raw.get("odno")) is not str
        or raw.get("odno") != order_no
        or type(raw.get("ord_tmd")) is not str
        or _TIME.fullmatch(raw["ord_tmd"]) is None
        or row.order_time != raw.get("ord_tmd")
        or raw.get("edge_command_id") is not None
    ):
        return "native_source_unproven", evidence
    qty = _decimal(row.quantity)
    price = _decimal(row.price)
    if qty is None or qty <= 0 or qty != qty.to_integral_value() or price is None:
        return "order_terms_invalid", evidence
    if row.order_type == "limit" and price > 0:
        evidence["ord_dvsn"] = "00"
    elif row.order_type == "market" and price == 0:
        evidence["ord_dvsn"] = "01"
    else:
        return "day_terms_unproven", evidence
    detail = row.last_reconcile_detail
    if type(detail) is not dict or detail.get("reason_code") not in _NO_FILL_REASONS:
        return "fill_unknown", evidence
    attributed = _decimal(detail.get("attributed_fill_qty"))
    if attributed is None:
        return "fill_unknown", evidence
    if attributed != 0:
        return "fill_recorded", evidence
    evidence["zero_fill_recorded"] = True
    trade_date = row.trade_date
    if not isinstance(trade_date, datetime) or trade_date.tzinfo is None:
        return "trade_date_unknown", evidence
    order_date = trade_date.astimezone(_KST).date()
    age, error = _session_age(order_date, today, calendar)
    if error is not None:
        return error, evidence
    evidence["age_sessions"] = age
    if type(min_sessions) is not int or not 2 <= min_sessions <= 20:
        return "min_sessions_invalid", evidence
    if age is None or age < min_sessions:
        return "too_recent", evidence
    return "eligible", evidence


__all__ = [
    "RULE_VERSION",
    "MAX_LEDGER_IDS",
    "MAX_DECISION_REF",
    "MAX_STRATEGY",
    "classify_row",
    "validate_request",
]
