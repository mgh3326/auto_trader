"""Pure publish validation primitives for KR plan rows."""

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.core.symbol import to_db_symbol
from app.services.daily_plan.vocabulary import TRIGGER_KINDS, VENUE_SESSIONS

_DECIMAL = re.compile(r"[0-9]{1,38}(?:\.[0-9]{1,38})?")
_SIDES = {"new": "buy", "add": "buy", "trim": "sell", "risk_sell": "sell"}


def require_base_version(expected: int, current: int) -> None:
    if type(expected) is not int or expected < 0:
        raise ValueError("invalid_base_version")
    if expected != current:
        raise ValueError("stale_version")


def parse_positive_decimal(value: Any) -> Decimal:
    if type(value) is not str or not _DECIMAL.fullmatch(value):
        raise ValueError("invalid_decimal")
    number = Decimal(value)
    if number <= 0:
        raise ValueError("invalid_decimal")
    return number


def normalize_symbol(symbol: str, market: str, venue: str, session: str) -> str:
    normalized = to_db_symbol(symbol.strip().upper())
    if market != "kr" or not re.fullmatch(r"[0-9]{6}", normalized):
        raise ValueError("invalid_symbol")
    if session not in VENUE_SESSIONS.get(venue, ()):
        raise ValueError("invalid_venue_session")
    return normalized


def validate_side(action: str, side: str | None) -> None:
    if side != _SIDES.get(action):
        raise ValueError("invalid_side")


def validate_condition(kind: str, value: str) -> None:
    if kind not in TRIGGER_KINDS:
        raise ValueError("invalid_condition")
    if kind == "event":
        if not value.strip() or len(value) > 500:
            raise ValueError("invalid_event")
    else:
        parse_positive_decimal(value)


def input_reason(
    sources: Mapping[str, Mapping[str, datetime | None]],
    *,
    required: frozenset[str],
    bundle_closed_at: datetime,
    oldest_accepted_at: datetime,
) -> str | None:
    """Use value-level observation evidence and the server's freshness boundary."""
    reasons: set[str] = set()
    for source in required:
        facts = sources.get(source)
        if facts is None:
            reasons.add("input_missing")
            continue
        event_at = facts.get("event_at")
        observed_at = facts.get("first_observed_at")
        if (
            event_at is None
            or observed_at is None
            or event_at.utcoffset() is None
            or observed_at.utcoffset() is None
        ):
            reasons.add("observed_time_unproven")
            continue
        if max(event_at, observed_at) > bundle_closed_at:
            raise ValueError("input_after_bundle_cutoff")
        if event_at < oldest_accepted_at:
            reasons.add("input_stale")
    return next(
        (
            reason
            for reason in ("input_missing", "observed_time_unproven", "input_stale")
            if reason in reasons
        ),
        None,
    )


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("observed_time_unproven")
    return parsed


def row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str | None]:
    return (row["row_kind"], row["market"], row["symbol"], row.get("account_ref"))


def prepare_row(
    raw: dict[str, Any],
    *,
    evidence: dict[str, Any] | None,
    asof: datetime,
    session_close: datetime,
    bundle_closed_at: datetime,
    oldest_accepted_at: datetime,
    no_add_floor_pct: Decimal,
) -> dict[str, Any]:
    from app.services.daily_plan.vocabulary import ACTION_BASES, ACTIONS, ROW_KINDS

    row = dict(raw)
    row["symbol"] = normalize_symbol(
        row["symbol"], row["market"], row["venue"], row["session"]
    )
    kind, _, _, account_ref = row_key(row)
    if kind not in ROW_KINDS or (kind == "holding") != bool(account_ref):
        raise ValueError("invalid_account_attribution")
    if kind == "candidate" and account_ref is not None:
        raise ValueError("invalid_account_attribution")
    if account_ref and not re.fullmatch(r"[0-9a-f]{64}", account_ref):
        raise ValueError("invalid_account_attribution")
    action = row["action"]
    if action not in ACTIONS or row["action_basis"] not in ACTION_BASES:
        raise ValueError("invalid_action")
    if kind == "candidate" and action not in ("new", "watch", "hold"):
        raise ValueError("invalid_action")
    if kind == "holding" and action == "new":
        raise ValueError("invalid_action")
    validate_side(action, row.get("side"))
    thesis = row["thesis"]
    if type(thesis) is not str or not thesis.strip() or len(thesis) > 1000:
        raise ValueError("invalid_thesis")

    until = parse_time(row["valid_until"]) if row.get("valid_until") else session_close
    if not asof < until <= session_close:
        raise ValueError("invalid_expiry")
    row["valid_until"] = until.isoformat()
    conditions = row.get("invalidation_conditions", [])
    if type(conditions) is not list or len(conditions) > 3:
        raise ValueError("invalid_conditions")
    trigger = row.get("trigger")
    for condition in ([trigger] if trigger else []) + conditions:
        validate_condition(condition["kind"], condition["value"])

    for field in (
        "max_buy_price",
        "min_sell_price",
        "would_be_limit_price",
        "would_be_quantity",
        "would_be_notional",
        "planned_amount",
    ):
        if row.get(field) is not None:
            parse_positive_decimal(row[field])
    if action in ("new", "add"):
        if row.get("max_buy_price") is None or row.get("would_be_limit_price") is None:
            raise ValueError("buy_price_required")
        if (row.get("would_be_quantity") is None) == (
            row.get("would_be_notional") is None
        ):
            raise ValueError("invalid_buy_size")
        if parse_positive_decimal(row["would_be_limit_price"]) > parse_positive_decimal(
            row["max_buy_price"]
        ):
            raise ValueError("buy_limit_above_maximum")
    if action in ("trim", "risk_sell") and row.get("min_sell_price") is None:
        raise ValueError("sell_price_required")

    sources = (evidence or {}).get("sources", {})
    source_times = {
        name: {
            key: parse_time(value) if value is not None else None
            for key, value in source.items()
            if key in ("event_at", "first_observed_at")
        }
        for name, source in sources.items()
    }
    required = frozenset({"price", "holdings"} if kind == "holding" else {"price"})
    if action in ("add", "trim", "risk_sell"):
        required |= {"position", "open_orders", "protected_quantity"}
    reason = input_reason(
        source_times,
        required=required,
        bundle_closed_at=bundle_closed_at,
        oldest_accepted_at=oldest_accepted_at,
    )
    row["input_asof"] = {
        source: {
            key: value.isoformat() if value else None for key, value in times.items()
        }
        for source, times in source_times.items()
    }
    row["not_executable_reason"] = reason
    if kind == "holding" and evidence is None:
        raise ValueError("invalid_account_attribution")
    if reason:
        row.update(action="watch", action_basis="scored", side=None, reason_code=reason)
        if kind == "candidate":
            row["disposition"] = "watch"
    elif kind == "holding" and evidence:
        if evidence["account_scope"] == "read_only" and action not in ("watch", "hold"):
            raise ValueError("account_not_routable")
        if action == "add":
            costs = evidence.get("average_costs", [])
            if not costs:
                row["not_executable_reason"] = "position_unknown"
            else:
                close = parse_positive_decimal(evidence["last_close"])
                if any(
                    close * 100
                    <= parse_positive_decimal(cost) * (100 + no_add_floor_pct)
                    for cost in costs
                ):
                    row["not_executable_reason"] = "add_blocked_lot_at_or_below_floor"
        if action == "risk_sell":
            # Risk-sale evidence must come from the server's evaluator, never from the proposal.
            if row["action_basis"] != "rule" or evidence.get(
                "risk_sell_quantity"
            ) != row.get("would_be_quantity"):
                raise ValueError("risk_rule_mismatch")
            if not evidence.get("risk_sell_quantity"):
                raise ValueError("risk_rule_mismatch")
    if row["action"] == "watch":
        if (
            trigger is None
            or not conditions
            or (kind == "candidate" and row.get("max_buy_price") is None)
        ):
            raise ValueError("watch_conditions_required")
    if type(row.get("carry_allowed", False)) is not bool:
        raise ValueError("invalid_carry_rule")
    row.setdefault("carry_allowed", False)
    row["carry_count"] = 0
    return row
