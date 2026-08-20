"""Session window + holiday skip for ROB-1297.

Reuses ``app.services.market_events.session_calendar`` (XKRX/XNYS via
``exchange_calendars``) — the same calendar family as the KR/US holiday skip
used by research-run refresh / invest screener. Does not invent a new holiday
table.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from app.services.market_close_digest.types import Market
from app.services.market_events.session_calendar import is_trading_session

KST = ZoneInfo("Asia/Seoul")
ET = ZoneInfo("America/New_York")

# Documented intended Prefect cadence (NOT registered in this repo).
INTENDED_CRON_KST: dict[Market, tuple[int, int]] = {
    "us": (5, 5),
    "kr": (15, 45),
    "crypto": (9, 5),
}


def infer_session_date(market: Market, now: datetime) -> date:
    """Session date the digest covers when run at ``now``.

    US cron 05:05 KST is 16:05 ET the previous KST calendar day (EDT), so the
    ET date of ``now`` is the session that just closed. KR 15:45 KST is the
    same KST date. Crypto uses the last *completed* KST calendar day (the 24h
    window that has already ended by cron 09:05).
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    if market == "us":
        return now.astimezone(ET).date()
    kst_now = now.astimezone(KST)
    if market == "crypto":
        return (kst_now - timedelta(days=1)).date()
    return kst_now.date()


def session_window(
    market: Market,
    session_date: date,
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """UTC ``[start, end)`` covered by the digest.

    Local calendar day of the session (ET for US, KST for KR/crypto).
    ``end`` is clamped to ``now`` so a run never queries the future tail of
    that calendar day (that tail would otherwise be permanently skipped by
    the next run, which advances ``session_date``).
    """
    tz = ET if market == "us" else KST
    start_local = datetime.combine(session_date, time.min, tzinfo=tz)
    start = start_local.astimezone(UTC)
    end = start + timedelta(days=1)
    if now is not None:
        now_utc = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
        if now_utc < end:
            end = now_utc
    if end < start:
        end = start
    return start, end


def should_skip_holiday(market: Market, session_date: date) -> bool:
    """True when KR/US session_date is not a confirmed trading day.

    Crypto is 24x7 — never skipped for holiday.
    """
    if market == "crypto":
        return False
    return not is_trading_session(market, session_date)
