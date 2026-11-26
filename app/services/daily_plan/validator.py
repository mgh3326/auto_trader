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
