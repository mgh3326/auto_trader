"""Deterministic intraday market-session derivation (ROB-374 B6).

Maps a ``(market, instant)`` pair to the persisted ``MarketSessionLiteral``
vocabulary (``regular`` / ``pre`` / ``post`` / ``24x7``). The instant is the
bundle's recorded ``as_of`` — a real, captured market moment — so this is *not*
a wall-clock guess: ROB-366 B6 deliberately refused to invent a session from
``datetime.now()``, and that still holds. Deriving from the bundle's own as-of
time is the missing piece ROB-374 needs for ``intraday_action_report_v1``.

Trading-day classification (weekend / holiday / early-close half-day) comes from
the fail-closed XNYS / XKRX calendar in
:mod:`app.services.market_events.session_calendar`. Any instant that cannot be
positively classified — unknown market, missing timestamp, outside the
extended-hours envelope, or a non-session day — returns ``None`` rather than a
fabricated session.

Scope: US (XNYS) is classified across pre / regular / post; KR (XKRX) only
``regular`` (extended NXT windows are intentionally not fabricated here); crypto
is always ``24x7``. Anything else is ``None``.
"""

from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from app.schemas.investment_reports import MarketSessionLiteral
from app.services.market_events.session_calendar import regular_session_bounds

_UTC = ZoneInfo("UTC")
_EXCHANGE_TZ: dict[str, ZoneInfo] = {
    "us": ZoneInfo("America/New_York"),
    "kr": ZoneInfo("Asia/Seoul"),
}

# US extended-hours envelope (exchange-local). Regular bounds come from the
# calendar (so half-day early closes are honored); these frame pre / post.
_US_PRE_OPEN = time(4, 0)
_US_POST_CLOSE = time(20, 0)


def derive_market_session(
    market: str, at: datetime | None
) -> MarketSessionLiteral | None:
    """Classify ``at`` into the report market-session vocabulary, or ``None``.

    ``at`` is treated as UTC when naive. Returns ``None`` for an unknown market,
    a missing timestamp, a non-trading day, or an instant outside the supported
    session windows (fail-closed).
    """
    normalized = (market or "").strip().lower()
    if normalized == "crypto":
        return "24x7"
    tz = _EXCHANGE_TZ.get(normalized)
    if at is None or tz is None:
        return None

    at_utc = at if at.tzinfo is not None else at.replace(tzinfo=_UTC)
    local = at_utc.astimezone(tz)

    bounds = regular_session_bounds(normalized, local.date())  # type: ignore[arg-type]
    if bounds is None:
        return None  # weekend / holiday / out of range
    open_utc, close_utc = bounds
    if open_utc <= at_utc < close_utc:
        return "regular"

    if normalized == "us":
        pre_open = local.replace(
            hour=_US_PRE_OPEN.hour, minute=0, second=0, microsecond=0
        )
        post_close = local.replace(
            hour=_US_POST_CLOSE.hour, minute=0, second=0, microsecond=0
        )
        open_local = open_utc.astimezone(tz)
        close_local = close_utc.astimezone(tz)
        if pre_open <= local < open_local:
            return "pre"
        if close_local <= local < post_close:
            return "post"

    return None
