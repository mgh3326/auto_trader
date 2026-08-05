"""Read-only, deterministic input types for US Stage-B fixture and U1 wiring.

The source deliberately carries only the fields that the three frozen
candidates may consume: adjusted close, open, and volume.  There are no
high/low fields and no corpus loader, broker, database, or write path here.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Protocol

__all__ = [
    "AccessRecord",
    "ExplorationBoundaryAccessError",
    "ExplorationBoundaryAccessSpy",
    "InMemoryUSBarSource",
    "USBarSource",
    "USStageBInputError",
    "USStageBDailyBar",
]


class USStageBInputError(ValueError):
    """The source does not satisfy the minimal, read-only US Stage-B shape."""


class ExplorationBoundaryAccessError(RuntimeError):
    """An attempted source read fell outside the explicit exploration window."""


@dataclass(frozen=True)
class USStageBDailyBar:
    """One unmodified daily US observation; null/non-finite cells are preserved.

    Numeric cells are intentionally not cleaned or coerced here.  The signal
    engine sees them unchanged and excludes an observation when a required
    field is null, non-finite, or non-positive.
    """

    symbol: str
    session_date: date
    open: float | None
    adjusted_close: float | None
    volume: float | None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise USStageBInputError("US Stage-B bar requires a non-empty symbol")
        if isinstance(self.session_date, datetime) or not isinstance(
            self.session_date, date
        ):
            raise USStageBInputError("US Stage-B session_date must be a date")


class USBarSource(Protocol):
    """Point-read input protocol; source implementations are read-only."""

    def symbols(self) -> tuple[str, ...]:
        """Return source symbols as metadata, without a time-series read."""

    def get(self, symbol: str, session_date: date) -> USStageBDailyBar | None:
        """Return a true missing observation as ``None`` without filling it."""


@dataclass(frozen=True)
class AccessRecord:
    """One source read record, serializable for boundary review."""

    symbol: str
    session_date: date
    allowed: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "symbol": self.symbol,
            "session_date": self.session_date.isoformat(),
            "allowed": self.allowed,
        }


class InMemoryUSBarSource:
    """Fixture source with deterministic symbols and duplicate-row refusal."""

    def __init__(self, bars: Iterable[USStageBDailyBar]) -> None:
        rows: dict[tuple[str, date], USStageBDailyBar] = {}
        symbols: set[str] = set()
        for bar in bars:
            key = (bar.symbol, bar.session_date)
            if key in rows:
                raise USStageBInputError(
                    f"duplicate US Stage-B bar: {bar.symbol}/{bar.session_date}"
                )
            rows[key] = bar
            symbols.add(bar.symbol)
        self._rows = rows
        self._symbols = tuple(sorted(symbols))

    def symbols(self) -> tuple[str, ...]:
        return self._symbols

    def get(self, symbol: str, session_date: date) -> USStageBDailyBar | None:
        return self._rows.get((symbol, session_date))


class ExplorationBoundaryAccessSpy:
    """Fail before any point read outside the explicit exploration date bounds."""

    def __init__(
        self,
        source: USBarSource,
        *,
        exploration_start: date,
        exploration_end: date,
    ) -> None:
        if exploration_start > exploration_end:
            raise USStageBInputError("exploration boundary start is after end")
        self._source = source
        self._start = exploration_start
        self._end = exploration_end
        self._records: list[AccessRecord] = []

    def symbols(self) -> tuple[str, ...]:
        return self._source.symbols()

    def get(self, symbol: str, session_date: date) -> USStageBDailyBar | None:
        allowed = self._start <= session_date <= self._end
        record = AccessRecord(
            symbol=symbol,
            session_date=session_date,
            allowed=allowed,
        )
        self._records.append(record)
        if not allowed:
            raise ExplorationBoundaryAccessError(
                "US Stage-B source read outside exploration boundary: "
                f"{session_date.isoformat()} not in "
                f"{self._start.isoformat()}..{self._end.isoformat()}"
            )
        return self._source.get(symbol, session_date)

    @property
    def records(self) -> tuple[AccessRecord, ...]:
        return tuple(self._records)

    @property
    def outside_boundary_records(self) -> tuple[AccessRecord, ...]:
        return tuple(record for record in self._records if not record.allowed)

    def assert_no_outside_access(self) -> None:
        if self.outside_boundary_records:
            raise ExplorationBoundaryAccessError(
                "US Stage-B attempted outside-boundary source access"
            )

    def summary(self) -> dict[str, int]:
        return {
            "reads_total": len(self._records),
            "outside_boundary_reads": len(self.outside_boundary_records),
        }
