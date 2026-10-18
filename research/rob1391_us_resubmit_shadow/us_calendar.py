"""XNYS regular-session calendar (holidays and early closes), ET + UTC.

Only official regular sessions are counted (prereg §1). Session arithmetic is
index based: ``offset(d0, 1)`` is D1, ``offset(d0, 20)`` is D+20. Every instant
is stored twice, UTC and America/New_York, so an audit never has to redo a
timezone conversion.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

ET = ZoneInfo("America/New_York")
CALENDAR_NAME = "XNYS"


class CalendarError(RuntimeError):
    """A date or instant could not be mapped to the official calendar."""


def _to_utc(ts: pd.Timestamp) -> datetime:
    return ts.to_pydatetime().astimezone(UTC)


@dataclass(frozen=True, slots=True)
class Session:
    day: date
    open_utc: datetime
    close_utc: datetime

    @property
    def open_et(self) -> datetime:
        return self.open_utc.astimezone(ET)

    @property
    def close_et(self) -> datetime:
        return self.close_utc.astimezone(ET)

    @property
    def early_close(self) -> bool:
        return self.close_et.hour < 16

    def as_record(self) -> dict[str, str | bool]:
        return {
            "day": self.day.isoformat(),
            "open_utc": self.open_utc.isoformat(),
            "close_utc": self.close_utc.isoformat(),
            "open_et": self.open_et.isoformat(),
            "close_et": self.close_et.isoformat(),
            "early_close": self.early_close,
        }


class USCalendar:
    """Thin, index-based view over ``exchange_calendars`` XNYS."""

    def __init__(self, start: date, end: date) -> None:
        cal = xcals.get_calendar(
            CALENDAR_NAME, start=pd.Timestamp(start), end=pd.Timestamp(end)
        )
        sessions: list[Session] = []
        for label in cal.sessions:
            sessions.append(
                Session(
                    day=label.date(),
                    open_utc=_to_utc(cal.session_open(label)),
                    close_utc=_to_utc(cal.session_close(label)),
                )
            )
        if not sessions:
            raise CalendarError(f"no XNYS sessions between {start} and {end}")
        self._sessions = sessions
        self._days = [s.day for s in sessions]
        self._closes = [s.close_utc for s in sessions]
        self._opens = [s.open_utc for s in sessions]
        self.start = start
        self.end = end

    # ------------------------------------------------------------- lookups
    def is_session(self, day: date) -> bool:
        i = bisect.bisect_left(self._days, day)
        return i < len(self._days) and self._days[i] == day

    def index(self, day: date) -> int:
        i = bisect.bisect_left(self._days, day)
        if i >= len(self._days) or self._days[i] != day:
            raise CalendarError(f"{day} is not an XNYS session")
        return i

    def session(self, day: date) -> Session:
        return self._sessions[self.index(day)]

    def offset(self, day: date, n: int) -> date:
        """The ``n``-th session after ``day`` (``n=0`` is ``day`` itself)."""

        i = self.index(day) + n
        if i < 0 or i >= len(self._days):
            raise CalendarError(f"offset {n} from {day} leaves the loaded calendar")
        return self._days[i]

    def gap(self, earlier: date, later: date) -> int:
        """Session count from ``earlier`` to ``later`` (same session -> 0)."""

        return self.index(later) - self.index(earlier)

    def previous(self, day: date, n: int) -> list[date]:
        """The ``n`` sessions strictly before ``day``, oldest first."""

        i = self.index(day)
        if i - n < 0:
            raise CalendarError(f"fewer than {n} sessions loaded before {day}")
        return self._days[i - n : i]

    def working_session(self, instant: datetime) -> date:
        """First session whose regular close is after ``instant``.

        A DAY order placed overnight (KST daytime) works in the next regular
        session; one placed during a session works in that session.
        """

        if instant.tzinfo is None:
            raise CalendarError("naive datetime has no defined session")
        i = bisect.bisect_right(self._closes, instant.astimezone(UTC))
        if i >= len(self._days):
            raise CalendarError(f"{instant} is after the loaded calendar")
        return self._days[i]

    def first_session_opening_after(self, instant: datetime) -> date:
        """First session whose open is strictly after ``instant``."""

        if instant.tzinfo is None:
            raise CalendarError("naive datetime has no defined session")
        i = bisect.bisect_right(self._opens, instant.astimezone(UTC))
        if i >= len(self._days):
            raise CalendarError(f"{instant} is after the loaded calendar")
        return self._days[i]

    def window(self, start: date, n: int) -> list[date]:
        i = self.index(start)
        if i + n > len(self._days):
            raise CalendarError("window leaves the loaded calendar")
        return self._days[i : i + n]


@lru_cache(maxsize=8)
def calendar_for(anchor: date) -> USCalendar:
    """Calendar loaded wide enough for lookbacks and D+20 (+aux) horizons."""

    return USCalendar(anchor - timedelta(days=200), anchor + timedelta(days=200))


def exchange_calendars_version() -> str:
    return str(getattr(xcals, "__version__", "unknown"))
