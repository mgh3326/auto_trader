"""
Timezone utilities for the application.

This module provides KST (Korea Standard Time) as the default timezone
for all datetime operations throughout the application.
"""

from datetime import datetime, timedelta, timezone

# KST (한국 표준시, UTC+9)
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    """
    Get current datetime in KST.

    Returns:
        datetime: Current datetime with KST timezone
    """
    return datetime.now(KST)


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
