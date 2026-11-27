"""Pure publish validation primitives for KR plan rows."""

import re
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any

from app.core.symbol import to_db_symbol
from app.mcp_server.tick_size import get_tick_size_kr
from app.services.daily_plan.vocabulary import (
    ACCOUNT_MODES,
    ACCOUNT_REF_KINDS,
    ACCOUNT_SCOPES,
    ACTION_BASES,
    ACTIONS,
    DISPOSITIONS,
    REASON_CODES,
    ROW_KINDS,
    TRIGGER_KINDS,
    UNAVAILABLE_REASONS,
    VENUE_SESSIONS,
)

_DECIMAL = re.compile(r"[0-9]{1,38}(?:\.[0-9]{1,38})?")
_SIDES = {"new": "buy", "add": "buy", "trim": "sell", "risk_sell": "sell"}
# KR short code: exactly six digits or upper-case ASCII letters (hk 6769, hk 6959).
KR_SHORT_CODE = re.compile(r"[0-9A-Z]{6}")
# Loss-cut replay inputs, with the skip an unknown one means (design §7.2).
# Open-sell, protected quantity and bar volume are absent on purpose: the rule
# already reads an unknown one as unknown.
_RISK_INPUT_SKIPS = (
    ("quantity", "position_unreadable"),
    ("average_cost", "position_unreadable"),
    ("plan", "no_exit_plan"),
    ("bars", "price_missing"),
    ("price_fresh", "price_stale"),
    ("prior_decision", "position_unreadable"),
)


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
    if market != "kr" or not KR_SHORT_CODE.fullmatch(normalized):
        raise ValueError("invalid_symbol")
    if session not in VENUE_SESSIONS.get(venue, ()):
        raise ValueError("invalid_venue_session")
    return normalized


def validate_side(action: str, side: str | None) -> None:
    if side != _SIDES.get(action):
        raise ValueError("invalid_side")


def validate_condition(kind: str, value: str) -> None:
    if kind not in TRIGGER_KINDS or type(value) is not str:
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
    for source, facts in sources.items():
        event_at = facts.get("event_at")
        observed_at = facts.get("first_observed_at")
        for value in (event_at, observed_at):
            if (
                value is not None
                and value.utcoffset() is not None
                and value > bundle_closed_at
            ):
                raise ValueError("input_after_bundle_cutoff")
        if source not in required:
            continue
        if (
            event_at is None
            or observed_at is None
            or event_at.utcoffset() is None
            or observed_at.utcoffset() is None
        ):
            reasons.add("observed_time_unproven")
        elif event_at < oldest_accepted_at:
            reasons.add("input_stale")
    if required - sources.keys():
        reasons.add("input_missing")
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


def unavailable_reason(value: Any) -> str | None:
    """The closed reason of an ``{"unavailable": reason}`` value, else ``None``.

    A reason outside the closed list is still unknown, never a usable value.
    """
    if not isinstance(value, Mapping) or "unavailable" not in value:
        return None
    reason = value["unavailable"]
    return reason if reason in UNAVAILABLE_REASONS else "input_missing"


def _first_unavailable(value: Any) -> str | None:
    """The first unavailable value anywhere inside ``value``, skipping volume."""
    if reason := unavailable_reason(value):
        return reason
    if isinstance(value, Mapping):
        children = [item for key, item in value.items() if key != "volume"]
    elif isinstance(value, list):
        children = value
    else:
        return None
    return next(
        (reason for item in children if (reason := _first_unavailable(item))), None
    )


def risk_input_gap(raw: Any) -> tuple[str, str] | None:
    """``(default skip, reason)`` of the first unknown loss-cut replay input."""
    if reason := unavailable_reason(raw):
        return "position_unreadable", reason
    if not isinstance(raw, Mapping):
        return None
    for field, skip in _RISK_INPUT_SKIPS:
        if reason := _first_unavailable(raw.get(field)):
            return skip, reason
    return None


def _evidence_time(value: Any) -> datetime | None:
    if value is None or unavailable_reason(value):
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.utcoffset() is not None else None


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
    allowed = {
        "row_kind",
        "market",
        "symbol",
        "account_ref",
        "action",
        "action_basis",
        "venue",
        "session",
        "side",
        "thesis",
        "valid_until",
        "trigger",
        "invalidation_conditions",
        "max_buy_price",
        "min_sell_price",
        "would_be_limit_price",
        "would_be_quantity",
        "would_be_notional",
        "planned_amount",
        "carry_allowed",
        "carried_from_row_id",
        "disposition",
        "reason_code",
        "would_be_account_ref",
        "sector_key",
        "candidate_call_id",
        "candidate_row_id",
        "thesis_status",
        "grade_value",
        "grade_quality",
        "forecast_request",
    }
    if raw.keys() - allowed:
        raise ValueError("unknown_row_field")
    row = dict(raw)
    row["symbol"] = normalize_symbol(
        row["symbol"], row["market"], row["venue"], row["session"]
    )
    kind, _, _, account_ref = row_key(row)
    if kind not in ROW_KINDS or (kind == "holding") != bool(account_ref):
        raise ValueError("invalid_account_attribution")
    if kind == "candidate" and account_ref is not None:
        raise ValueError("invalid_account_attribution")
    if account_ref and not re.fullmatch(
        r"(?:acct_[0-9a-f]{32}|[0-9a-f]{64})", account_ref
    ):
        raise ValueError("invalid_account_attribution")
    action = row["action"]
    if action not in ACTIONS or row["action_basis"] not in ACTION_BASES:
        raise ValueError("invalid_action")
    if kind == "candidate" and action not in ("new", "add", "watch", "hold"):
        raise ValueError("invalid_action")
    if kind == "holding" and action == "new":
        raise ValueError("invalid_action")
    validate_side(action, row.get("side"))
    if row.get("disposition") is not None and (
        kind != "candidate" or row["disposition"] not in DISPOSITIONS
    ):
        raise ValueError("invalid_disposition")
    if row.get("reason_code") is not None and row["reason_code"] not in REASON_CODES:
        raise ValueError("invalid_reason")
    if row.get("sector_key") is not None and (
        type(row["sector_key"]) is not str or len(row["sector_key"]) > 100
    ):
        raise ValueError("invalid_sector")
    would_be_ref = row.get("would_be_account_ref")
    if would_be_ref is not None:
        account_mode = (
            (evidence or {}).get("routable_account_modes", {}).get(would_be_ref)
        )
        if (
            not re.fullmatch(r"acct_[0-9a-f]{32}", would_be_ref)
            or account_mode not in ACCOUNT_MODES
        ):
            raise ValueError("invalid_account_attribution")
        row["would_be_account_mode"] = account_mode
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
    for condition in ([trigger] if trigger is not None else []) + conditions:
        if type(condition) is not dict or condition.keys() != {"kind", "value"}:
            raise ValueError("invalid_condition")
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
            key: _evidence_time(value)
            for key, value in source.items()
            if key in ("event_at", "first_observed_at")
        }
        for name, source in sources.items()
        if source.get("unavailable") not in ("input_missing", "price_missing")
    }
    required = frozenset({"price", "holdings"} if kind == "holding" else {"price"})
    if kind == "holding" and action in ("add", "trim", "risk_sell"):
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
    # An absent source outranks a stated reason; a reason stated in place of a
    # time outranks the generic one the missing timestamp would produce.
    if reason != "input_missing":
        reason = next(
            (
                stated
                for name in sorted(required & sources.keys())
                for key in ("event_at", "first_observed_at")
                if (stated := unavailable_reason(sources[name].get(key)))
            ),
            reason,
        )
    if reason is None and any(
        source.get("unavailable") == "price_stale"
        for name, source in sources.items()
        if name in required
    ):
        reason = "input_stale"
    if reason is None and any(sources[name].get("unavailable") for name in required):
        reason = "input_missing"
    if kind == "holding" and evidence is not None:
        risk_inputs = evidence.get("risk_inputs") or {}
        if reason is None:
            gap = risk_input_gap(risk_inputs)
            reason = gap[1] if gap else None
        if action in ("add", "trim", "risk_sell") and reason is None:
            for field in ("open_sell_quantity", "protected_quantity"):
                quantity = risk_inputs.get(field) or {}
                if "known" not in quantity:
                    reason = unavailable_reason(quantity) or "input_missing"
                    break
        for field, vocabulary in (
            ("account_mode", ACCOUNT_MODES),
            ("account_scope", ACCOUNT_SCOPES),
            ("account_ref_kind", ACCOUNT_REF_KINDS),
        ):
            if evidence.get(field) not in vocabulary:
                raise ValueError("invalid_account_attribution")
            row[field] = evidence[field]
        if evidence["account_scope"] == "read_only" and action not in ("watch", "hold"):
            reason = reason or "account_not_routable"
        if action == "add" and reason is None:
            costs = evidence.get("average_costs", [])
            if stated := _first_unavailable([costs, evidence.get("last_close")]):
                reason = stated
            elif not costs or evidence.get("last_close") is None:
                reason = "position_unknown"
            else:
                close = parse_positive_decimal(evidence["last_close"])
                with localcontext(prec=100):
                    if any(
                        close * 100
                        <= parse_positive_decimal(cost) * (100 + no_add_floor_pct)
                        for cost in costs
                    ):
                        row["not_executable_reason"] = (
                            "add_blocked_lot_at_or_below_floor"
                        )
    if reason:
        row.update(
            action="watch",
            action_basis="scored",
            side=None,
            reason_code=reason,
            not_executable_reason=reason,
        )
        if kind == "candidate":
            row["disposition"] = "watch"
    row.setdefault("not_executable_reason", None)
    if (
        row.get("disposition") == "adopted"
        and would_be_ref is None
        and not row["not_executable_reason"]
    ):
        row["not_executable_reason"] = "account_attribution_unknown"
    if row["action"] in ("new", "add"):
        limit = parse_positive_decimal(row["would_be_limit_price"])
        size = row.get("would_be_quantity")
        limit_int = int(limit)
        security_type = (evidence or {}).get("security_type")
        if limit != limit_int or limit_int % get_tick_size_kr(limit_int, security_type):
            raise ValueError("tick_grid_violation")
        if size is not None:
            quantity = parse_positive_decimal(size)
            if quantity != int(quantity):
                raise ValueError("invalid_kr_quantity")
        if size is None and parse_positive_decimal(row["would_be_notional"]) < limit:
            raise ValueError("below_min_order_amount")
    if row["action"] == "watch":
        if reason and trigger is None:
            max_buy = row.get("max_buy_price")
            trigger = row["trigger"] = (
                {"kind": "price_at_or_below", "value": max_buy}
                if max_buy is not None
                else {"kind": "event", "value": "inputs_refreshed"}
            )
        if reason and not conditions:
            conditions = row["invalidation_conditions"] = [
                {"kind": "event", "value": "session_closed"}
            ]
        if (
            trigger is None
            or not conditions
            or (
                reason is None
                and kind == "candidate"
                and row.get("max_buy_price") is None
            )
        ):
            raise ValueError("watch_conditions_required")
    if type(row.get("carry_allowed", False)) is not bool:
        raise ValueError("invalid_carry_rule")
    row.setdefault("carry_allowed", False)
    row["carry_count"] = 0
    return row
