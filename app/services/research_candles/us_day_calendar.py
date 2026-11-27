"""hk 7044: the calendar-only read that labels KIS_DAY bars ``US_DAY``.

The research-minutes ingest router must not import the Toss brokers package.
Its client is order-capable, and the static guard denies that prefix from the
router's import closure. The router therefore reads the Toss US market calendar
only through this module, and this module imports nothing from
``app.services.brokers``.

The concrete reader is registered once at app startup (``app.main.create_app``),
outside the router's closure. The router sees only ``read_us_calendar``. With no
reader registered, or on any failure, the answer is ``None``. The router then
refuses KIS_DAY bars as ``calendar_unavailable``, never a guess (rows are
insert-only).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

logger = logging.getLogger(__name__)

#: ``day`` (KST calendar date) -> the Toss US market calendar, or ``None``.
UsCalendarReader = Callable[[date], Awaitable[Any | None]]

_reader: UsCalendarReader | None = None


def register_us_calendar_reader(reader: UsCalendarReader | None) -> None:
    """Install the process-wide calendar reader (startup wiring only)."""
    global _reader
    _reader = reader


async def read_us_calendar(day: date) -> Any | None:
    """The Toss US calendar for ``day``, or ``None`` when it cannot be read."""
    reader = _reader
    if reader is None:
        return None
    try:
        return await reader(day)
    except Exception as exc:  # noqa: BLE001 - reported as calendar_unavailable
        logger.info(
            "us day calendar unavailable for %s error=%s",
            day,
            type(exc).__name__,
        )
        return None


__all__ = ["UsCalendarReader", "read_us_calendar", "register_us_calendar_reader"]
