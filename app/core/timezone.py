"""
Timezone utilities for the application.

This module provides KST (Korea Standard Time) as the default timezone
for all datetime operations throughout the application.
"""

from datetime import UTC, datetime, timedelta, timezone

# KST (한국 표준시, UTC+9)
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """
    Get current datetime in KST.

    Returns:
        datetime: Current datetime with KST timezone
    """
    return datetime.now(KST)


def now_kst_naive() -> datetime:
    """
    Get current datetime in KST as a naive datetime (no tzinfo).

    Use this for DB columns that are TIMESTAMP WITHOUT TIME ZONE
    where KST is the assumed timezone convention.
    """
    return datetime.now(KST).replace(tzinfo=None)


def to_kst_naive(dt: datetime) -> datetime:
    """
    Convert a datetime to KST naive.

    - aware datetime → convert to KST, then strip tzinfo
    - naive datetime → returned as-is (assumed to be KST already)
    """
    if dt.tzinfo is not None:
        return dt.astimezone(KST).replace(tzinfo=None)
    return dt


def trade_day_kst(dt: datetime) -> str:
    """Return the KST trade day for an instant persisted as TIMESTAMPTZ.

    Execution-ledger timestamps are instants.  Treat legacy naive values as UTC
    rather than letting the host/session timezone change their reported day.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(KST).strftime("%Y%m%d")


def kst_day_window(dt: datetime) -> tuple[datetime, datetime]:
    """Return the [start, end) aware bounds of the KST calendar day holding ``dt``.

    ``filled_at`` and friends are TIMESTAMPTZ instants, so the window is built in
    KST and returned as tz-aware bounds suitable for direct SQL comparisons.
    Naive input is treated as UTC (same convention as ``trade_day_kst``).
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    start = dt.astimezone(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def format_datetime(dt: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """
    Format datetime to string. If no datetime is provided, use current KST time.

    Args:
        dt: datetime object to format (default: current KST time)
        fmt: strftime format string

    Returns:
        str: Formatted datetime string
    """
    if dt is None:
        dt = now_kst()
    return dt.strftime(fmt)
