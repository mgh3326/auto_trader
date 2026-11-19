"""Pure rules of the morning approval batch (hk 3795 §5.1, ticket T3a).

Standard library only, plus an injected session calendar. No database, no network.

* the target session T of a batch and its two deadlines;
* the effective session of an approval (Q4 = B), as the §5.1 session table;
* the compact callback data of the ``mpb`` card and the nonce digest.

A nonce is a random token that exists in clear only inside one card's callback data.
The database keeps its digest; the decision event that answers the card is what
spends it (a plan of a batch has at most one answer).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

#: Calendar days searched for a session; enough for the longest closure cluster.
SEARCH_DAYS = 32
#: Local zone of each market, to read the calendar day of ``now``.
MARKET_ZONE: dict[str, ZoneInfo] = {
    "equity_kr": ZoneInfo("Asia/Seoul"),
    "equity_us": ZoneInfo("America/New_York"),
}
CALLBACK_PREFIX = "mpb"
CALLBACK_ACTIONS: dict[str, str] = {"a": "approve", "r": "reject"}
_ACTION_CODE = {name: code for code, name in CALLBACK_ACTIONS.items()}
_NONCE_RE = re.compile(r"[A-Za-z0-9_-]{22}")
_CALLBACK_RE = re.compile(
    r"mpb:(?P<action>[ar]):(?P<member>[0-9a-z]{1,13}):(?P<nonce>[A-Za-z0-9_-]{22})"
)
MAX_CALLBACK_BYTES = 64
_NONCE_DOMAIN = b"mandate-plan-batch-nonce/v1:"


class NoSession(LookupError):
    """The calendar could not give a session; the caller fails closed."""


class SessionCalendar(Protocol):
    def bounds(self, market: str, day: date) -> tuple[datetime, datetime] | None:
        """UTC (open, close) of the regular session on ``day``, None when none."""


class ExchangeCalendar:
    """The production calendar: ``exchange_calendars`` through the market-events helper."""

    _KEY = {"equity_kr": "kr", "equity_us": "us"}

    def bounds(self, market: str, day: date) -> tuple[datetime, datetime] | None:
        from app.services.market_events.session_calendar import regular_session_bounds

        return regular_session_bounds(self._KEY[market], day)  # type: ignore[arg-type]


@dataclass(frozen=True)
class BatchWindow:
    """The target session of a batch and its two deadlines."""

    target_session: date
    approval_deadline_at: datetime  # open of the target session
    expires_at: datetime  # close of the target session


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} needs a time zone")
    return value


def batch_window(market: str, now: datetime, calendar: SessionCalendar) -> BatchWindow:
    """T = the first regular session that opens after ``now`` (§5.1).

    Created during a session, a batch targets the next trading day. The approval
    deadline is T's open (later approvals take effect after T) and the expiry is
    T's close (nothing is answered after it).
    """
    zone = MARKET_ZONE[market]
    start = _aware(now, "now").astimezone(zone).date()
    for offset in range(SEARCH_DAYS + 1):
        day = start + timedelta(days=offset)
        bounds = calendar.bounds(market, day)
        if bounds is not None and bounds[0] > now:
            return BatchWindow(day, bounds[0], bounds[1])
    raise NoSession(f"no {market} session opens within {SEARCH_DAYS} days of {now}")


def next_session(market: str, day: date, calendar: SessionCalendar) -> date:
    """The first trading session strictly after ``day``."""
    for offset in range(1, SEARCH_DAYS + 1):
        candidate = day + timedelta(days=offset)
        if calendar.bounds(market, candidate) is not None:
            return candidate
    raise NoSession(f"no {market} session within {SEARCH_DAYS} days after {day}")


def effective_session(
    market: str,
    now: datetime,
    window: BatchWindow,
    calendar: SessionCalendar,
) -> date | None:
    """When an approval given at ``now`` starts to count (Q4 = B), None once expired.

    Before the approval deadline: the target session. From the deadline until the
    expiry: the trading day after it. At and after the expiry: no effect.
    """
    moment = _aware(now, "now")
    if moment >= window.expires_at:
        return None
    if moment < window.approval_deadline_at:
        return window.target_session
    return next_session(market, window.target_session, calendar)


# ----------------------------------------------------------------- nonce / callback
def new_nonce() -> str:
    """22 URL-safe characters of fresh randomness (128 bits)."""
    return secrets.token_urlsafe(16)


def nonce_digest(nonce: str) -> str:
    if _NONCE_RE.fullmatch(nonce) is None:
        raise ValueError("a nonce is 22 URL-safe characters")
    return hashlib.sha256(_NONCE_DOMAIN + nonce.encode("ascii")).hexdigest()


def nonce_matches(nonce: str, stored_digest: str) -> bool:
    """Constant-time comparison of a presented nonce with a stored digest."""
    try:
        presented = nonce_digest(nonce)
    except ValueError:
        return False
    return hmac.compare_digest(presented, stored_digest)


def _base36(value: int) -> str:
    if value <= 0:
        raise ValueError("a card id is positive")
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        value, rest = divmod(value, 36)
        out = digits[rest] + out
    return out


@dataclass(frozen=True)
class ParsedCallback:
    action: str  # "approve" | "reject"
    member_id: int
    nonce: str


def build_callback_data(action: str, member_id: int, nonce: str) -> str:
    if action not in _ACTION_CODE:
        raise ValueError("action is approve or reject")
    if _NONCE_RE.fullmatch(nonce) is None:
        raise ValueError("a nonce is 22 URL-safe characters")
    data = f"{CALLBACK_PREFIX}:{_ACTION_CODE[action]}:{_base36(member_id)}:{nonce}"
    if len(data.encode("ascii")) > MAX_CALLBACK_BYTES:
        raise ValueError("callback data exceeds 64 bytes")
    return data


def is_mandate_callback_data(data: object) -> bool:
    """True when the data is addressed to this path (``mpb:`` prefix), valid or not."""
    return isinstance(data, str) and data.startswith(f"{CALLBACK_PREFIX}:")


def parse_callback_data(data: object) -> ParsedCallback | None:
    """Parse ``mpb:<a|r>:<card id base36>:<nonce>``; None when it is not exactly that."""
    if not isinstance(data, str) or len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return None
    match = _CALLBACK_RE.fullmatch(data)
    if match is None:
        return None
    member_id = int(match.group("member"), 36)
    if member_id <= 0:
        return None
    return ParsedCallback(
        CALLBACK_ACTIONS[match.group("action")], member_id, match.group("nonce")
    )


def inline_keyboard(member_id: int, nonce: str) -> dict:
    """The two buttons of one card."""
    return {
        "inline_keyboard": [
            [
                {
                    "text": "승인",
                    "callback_data": build_callback_data("approve", member_id, nonce),
                },
                {
                    "text": "기각",
                    "callback_data": build_callback_data("reject", member_id, nonce),
                },
            ]
        ]
    }
