"""KR quote price kind selection (hk task 6498).

Pure functions only: no I/O, no broker or DB imports. The caller reads the
venue orderbooks and last trades and passes them in.

Rule: an expected (예상체결) price is the representative price only during an
auction phase where no continuous trading happens. In continuous trading the
representative price is the venue's last trade in that session, else the firm
mid of a two-sided book, else unavailable. A one-sided book is never a price.
An expected price is always kept in its own field.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

from app.services.brokers.kis.live_order_expiry import (
    KR_PREMARKET_CLOSE_KST,
    KR_PREMARKET_OPEN_KST,
    KR_REGULAR_CLOSE_KST,
    KR_REGULAR_OPEN_KST,
    KRX_AFTER_CLOSE_KST,
    KRX_AFTER_OPEN_KST,
    NXT_CLOSE_KST,
)

KST = ZoneInfo("Asia/Seoul")

PRICE_KIND_LAST_TRADE = "last_trade"
PRICE_KIND_MID = "mid"
PRICE_KIND_EXPECTED = "expected"
PRICE_KIND_UNAVAILABLE = "unavailable"

PRICE_SESSION_REGULAR = "regular"
PRICE_SESSION_KRX_AFTER = "krx_after"
PRICE_SESSION_NXT_PRE = "nxt_pre"
PRICE_SESSION_NXT_MAIN = "nxt_main"
PRICE_SESSION_NXT_POST = "nxt_post"
PRICE_SESSION_AUCTION = "auction"

REASON_EXPECTED_NOT_ORDER_BASIS = "expected_price_not_order_basis"
REASON_AUCTION_WITHOUT_EXPECTED = "auction_without_expected_price"
REASON_NO_TRADE_ONE_SIDED = "no_trade_and_one_sided_book"
REASON_NO_TRADE_NO_BOOK = "no_trade_and_no_book"
REASON_SESSION_UNKNOWN = "session_unknown"

# Session boundaries come from the same source as research-candle labels.
NXT_PRE_START = KR_PREMARKET_OPEN_KST
NXT_PRE_END = KR_PREMARKET_CLOSE_KST
KRX_REGULAR_START = KR_REGULAR_OPEN_KST
NXT_POST_START = KRX_AFTER_OPEN_KST
NXT_POST_END = NXT_CLOSE_KST

# A last trade counts only when it is at most this old, the same bound as the
# orderbook freshness gate. An older trade is not a live price; the firm mid of a
# live two-sided book is used instead.
TRADE_MAX_AGE = dt.timedelta(minutes=5)

# KRX single-price auctions: pre-open call and closing call. Schedule assumption,
# not checked against an exchange notice in this change.
KRX_AUCTION_WINDOWS: tuple[tuple[dt.time, dt.time], ...] = (
    (dt.time(8, 30), dt.time(9, 0)),
    (dt.time(15, 20), dt.time(15, 30)),
)


def quote_session_at(now: dt.datetime, venue: str) -> str | None:
    """Return the quote session for a venue at an aware instant, or ``None``.

    The caller establishes that the KST date is a trading session. These time
    windows intentionally share their boundaries with research minute labels.
    """
    if not _is_aware(now):
        return None
    local = now.astimezone(KST).time().replace(tzinfo=None)
    if venue == "krx":
        if KR_REGULAR_OPEN_KST <= local < KR_REGULAR_CLOSE_KST:
            return PRICE_SESSION_REGULAR
        if KRX_AFTER_OPEN_KST <= local < KRX_AFTER_CLOSE_KST:
            return PRICE_SESSION_KRX_AFTER
    elif venue == "nxt":
        if NXT_PRE_START <= local < NXT_PRE_END:
            return PRICE_SESSION_NXT_PRE
        if NXT_POST_START <= local < NXT_POST_END:
            return PRICE_SESSION_NXT_POST
    return None


@dataclass(frozen=True, slots=True)
class VenueBook:
    """Best bid/ask of one venue. ``observed_at`` is the book's provider time."""

    venue: str
    best_bid: float | None
    best_ask: float | None
    observed_at: dt.datetime | None
    expected_price: float | None = None


@dataclass(frozen=True, slots=True)
class LastTrade:
    """A venue's most recent trade with its provider trade time (aware)."""

    venue: str
    price: float | None
    traded_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class PriceSelection:
    price_kind: str
    price: float | None
    price_venue: str | None
    price_as_of: dt.datetime | None
    price_session: str | None
    expected_price: float | None
    expected_price_venue: str | None
    reason: str | None
    venues: dict[str, dict[str, Any]]


def positive_price(value: Any) -> float | None:
    """A finite positive number, else None. Bools and strings are rejected."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def _is_aware(value: dt.datetime | None) -> bool:
    return value is not None and value.tzinfo is not None


def in_krx_auction(now: dt.datetime) -> bool:
    """True when ``now`` (aware) falls in a KRX single-price auction window."""
    if not _is_aware(now):
        return False
    local = now.astimezone(KST).time().replace(tzinfo=None)
    return any(start <= local < end for start, end in KRX_AUCTION_WINDOWS)


def nxt_price_session(overlay_session: str | None) -> tuple[str | None, dt.time | None]:
    """Map the NXT overlay session label to (price_session, session start time).

    ``nxt_premarket`` -> nxt_pre, ``nxt_after`` -> nxt_post. Any other label has
    no continuous NXT price session here, so it returns ``(None, None)``.
    """
    if overlay_session == "nxt_premarket":
        return PRICE_SESSION_NXT_PRE, NXT_PRE_START
    if overlay_session == "nxt_after":
        return PRICE_SESSION_NXT_POST, NXT_POST_START
    return None, None


def _render_venue(book: VenueBook, trade: LastTrade | None) -> dict[str, Any]:
    rendered: dict[str, Any] = {
        "best_bid": positive_price(book.best_bid),
        "best_ask": positive_price(book.best_ask),
        "observed_at": book.observed_at.isoformat()
        if _is_aware(book.observed_at)
        else None,
        "expected_price": positive_price(book.expected_price),
    }
    if trade is not None:
        rendered["last_trade_price"] = positive_price(trade.price)
        rendered["last_trade_at"] = (
            trade.traded_at.isoformat() if _is_aware(trade.traded_at) else None
        )
    return rendered


def _unavailable(
    *,
    reason: str,
    session: str | None,
    expected: float | None,
    expected_venue: str | None,
    venues: dict[str, dict[str, Any]],
) -> PriceSelection:
    return PriceSelection(
        price_kind=PRICE_KIND_UNAVAILABLE,
        price=None,
        price_venue=None,
        price_as_of=None,
        price_session=session,
        expected_price=expected,
        expected_price_venue=expected_venue,
        reason=reason,
        venues=venues,
    )


def _valid_trade(
    trade: LastTrade | None,
    *,
    session_start: dt.datetime,
    now: dt.datetime,
    max_trade_age: dt.timedelta,
) -> bool:
    if trade is None or positive_price(trade.price) is None:
        return False
    if not _is_aware(trade.traded_at):
        return False
    if not session_start <= trade.traded_at <= now:
        return False
    return now - trade.traded_at <= max_trade_age


def select_kr_price(
    *,
    session: str | None,
    session_start: dt.datetime | None,
    now: dt.datetime,
    venue_books: Mapping[str, VenueBook],
    last_trades: Mapping[str, LastTrade],
    primary_venue: str,
    max_trade_age: dt.timedelta = TRADE_MAX_AGE,
) -> PriceSelection:
    """Pick the representative price for one venue under the session rule.

    ``session`` is one of the ``PRICE_SESSION_*`` labels, or None when the
    session cannot be determined (then the price is unavailable). ``session_start``
    is the aware start of that session on today's KST date; a last trade counts
    only when it lies in ``[session_start, now]``. ``now`` must be aware.
    """
    venues = {
        venue: _render_venue(book, last_trades.get(venue))
        for venue, book in venue_books.items()
    }
    primary = venue_books.get(primary_venue)
    expected = positive_price(primary.expected_price) if primary else None
    expected_venue = primary_venue if expected is not None else None

    if session is None:
        return _unavailable(
            reason=REASON_SESSION_UNKNOWN,
            session=None,
            expected=expected,
            expected_venue=expected_venue,
            venues=venues,
        )
    if primary is None:
        return _unavailable(
            reason=REASON_NO_TRADE_NO_BOOK,
            session=session,
            expected=None,
            expected_venue=None,
            venues=venues,
        )

    if session == PRICE_SESSION_AUCTION:
        if expected is not None:
            return PriceSelection(
                price_kind=PRICE_KIND_EXPECTED,
                price=expected,
                price_venue=primary_venue,
                price_as_of=primary.observed_at,
                price_session=session,
                expected_price=expected,
                expected_price_venue=expected_venue,
                reason=REASON_EXPECTED_NOT_ORDER_BASIS,
                venues=venues,
            )
        return _unavailable(
            reason=REASON_AUCTION_WITHOUT_EXPECTED,
            session=session,
            expected=None,
            expected_venue=None,
            venues=venues,
        )

    # Continuous trading: expected never becomes the representative price here.
    trade = last_trades.get(primary_venue)
    if session_start is not None and _valid_trade(
        trade,
        session_start=session_start,
        now=now,
        max_trade_age=max_trade_age,
    ):
        assert trade is not None  # narrowed by _valid_trade
        return PriceSelection(
            price_kind=PRICE_KIND_LAST_TRADE,
            price=positive_price(trade.price),
            price_venue=primary_venue,
            price_as_of=trade.traded_at,
            price_session=session,
            expected_price=expected,
            expected_price_venue=expected_venue,
            reason=None,
            venues=venues,
        )

    bid = positive_price(primary.best_bid)
    ask = positive_price(primary.best_ask)
    if bid is not None and ask is not None:
        return PriceSelection(
            price_kind=PRICE_KIND_MID,
            price=(bid + ask) / 2.0,
            price_venue=primary_venue,
            price_as_of=primary.observed_at,
            price_session=session,
            expected_price=expected,
            expected_price_venue=expected_venue,
            reason=None,
            venues=venues,
        )
    reason = REASON_NO_TRADE_ONE_SIDED if (bid or ask) else REASON_NO_TRADE_NO_BOOK
    return _unavailable(
        reason=reason,
        session=session,
        expected=expected,
        expected_venue=expected_venue,
        venues=venues,
    )
