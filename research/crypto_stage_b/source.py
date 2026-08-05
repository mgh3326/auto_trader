"""Read-only daily-bar source interfaces and exploration-boundary access spy."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from research.crypto_corpus.loader import LabeledCorpus

__all__ = [
    "AccessRecord",
    "BoundaryAccessSpy",
    "CryptoStageBInputError",
    "DailyBar",
    "DailyBarSource",
    "ExplorationBoundaryAccessError",
    "InMemoryDailyBarSource",
    "TerminalEvent",
    "bars_from_crypto_corpus_records",
    "source_from_labeled_corpus",
]


class CryptoStageBInputError(ValueError):
    """The read-only Stage-B input does not meet the daily-corpus contract."""


class ExplorationBoundaryAccessError(RuntimeError):
    """A source read attempted a UTC day outside the explicit exploration window."""


@dataclass(frozen=True)
class DailyBar:
    """One raw crypto-corpus UTC daily bar; values stay uncleaned for exclusion."""

    venue: str
    symbol: str
    session: date
    open: float
    high: float
    low: float
    close: float
    base_volume: float
    quote_volume: float
    frequency: str = "1d"
    bucket_timezone: str = "UTC"

    def __post_init__(self) -> None:
        if not self.venue or not self.symbol:
            raise CryptoStageBInputError(
                "daily bar requires non-empty venue and symbol"
            )
        if isinstance(self.session, datetime) or not isinstance(self.session, date):
            raise CryptoStageBInputError(
                "daily bar session must be a UTC calendar date"
            )
        if self.frequency != "1d":
            raise CryptoStageBInputError("crypto Stage-B accepts daily bars only")
        if self.bucket_timezone != "UTC":
            raise CryptoStageBInputError("crypto Stage-B requires UTC daily buckets")


@dataclass(frozen=True)
class TerminalEvent:
    """An observed terminal event; only explicit delisting evidence is usable."""

    venue: str
    symbol: str
    session: date
    event_type: str = "delisted"

    def __post_init__(self) -> None:
        if self.event_type != "delisted":
            raise CryptoStageBInputError(
                "only explicit delisted terminal events are valid"
            )
        if isinstance(self.session, datetime) or not isinstance(self.session, date):
            raise CryptoStageBInputError("terminal-event session must be a UTC date")


class DailyBarSource(Protocol):
    """A point-read source so the engine can prove it did not read holdout days."""

    def symbols(self, venue: str) -> tuple[str, ...]:
        """Return immutable venue metadata, not daily-bar data."""

    def get(self, venue: str, symbol: str, session: date) -> DailyBar | None:
        """Return exactly one UTC daily bar, or a true missing day."""

    def terminal_event(
        self, venue: str, symbol: str, session: date
    ) -> TerminalEvent | None:
        """Return only an observed terminal event at this exact UTC day."""


@dataclass(frozen=True)
class AccessRecord:
    kind: str
    venue: str
    symbol: str
    session: date
    allowed: bool

    def to_dict(self) -> dict[str, str | bool]:
        return {
            "kind": self.kind,
            "venue": self.venue,
            "symbol": self.symbol,
            "session": self.session.isoformat(),
            "allowed": self.allowed,
        }


class InMemoryDailyBarSource:
    """Deterministic read-only fixture/source adapter with duplicate refusal."""

    def __init__(
        self,
        bars: Iterable[DailyBar],
        *,
        terminal_events: Iterable[TerminalEvent] = (),
    ) -> None:
        by_key: dict[tuple[str, str, date], DailyBar] = {}
        symbols: dict[str, set[str]] = {}
        for bar in bars:
            key = (bar.venue, bar.symbol, bar.session)
            if key in by_key:
                raise CryptoStageBInputError(
                    f"duplicate daily bar: {bar.venue}/{bar.symbol}/{bar.session}"
                )
            by_key[key] = bar
            symbols.setdefault(bar.venue, set()).add(bar.symbol)

        events_by_key: dict[tuple[str, str, date], TerminalEvent] = {}
        for event in terminal_events:
            key = (event.venue, event.symbol, event.session)
            if key in events_by_key:
                raise CryptoStageBInputError(
                    "duplicate terminal event: "
                    f"{event.venue}/{event.symbol}/{event.session}"
                )
            events_by_key[key] = event

        self._bars = by_key
        self._symbols = {
            venue: tuple(sorted(venue_symbols))
            for venue, venue_symbols in symbols.items()
        }
        self._events = events_by_key

    def symbols(self, venue: str) -> tuple[str, ...]:
        return self._symbols.get(venue, ())

    def get(self, venue: str, symbol: str, session: date) -> DailyBar | None:
        return self._bars.get((venue, symbol, session))

    def terminal_event(
        self, venue: str, symbol: str, session: date
    ) -> TerminalEvent | None:
        return self._events.get((venue, symbol, session))


class BoundaryAccessSpy:
    """Wrap a source and fail before every read outside an explicit UTC window."""

    def __init__(
        self,
        source: DailyBarSource,
        *,
        exploration_start: date,
        exploration_end: date,
    ) -> None:
        if exploration_start > exploration_end:
            raise CryptoStageBInputError("exploration boundary start is after end")
        self._source = source
        self._start = exploration_start
        self._end = exploration_end
        self._records: list[AccessRecord] = []

    def symbols(self, venue: str) -> tuple[str, ...]:
        return self._source.symbols(venue)

    def get(self, venue: str, symbol: str, session: date) -> DailyBar | None:
        self._record_or_refuse("bar", venue, symbol, session)
        return self._source.get(venue, symbol, session)

    def terminal_event(
        self, venue: str, symbol: str, session: date
    ) -> TerminalEvent | None:
        self._record_or_refuse("terminal_event", venue, symbol, session)
        return self._source.terminal_event(venue, symbol, session)

    @property
    def records(self) -> tuple[AccessRecord, ...]:
        return tuple(self._records)

    @property
    def outside_boundary_records(self) -> tuple[AccessRecord, ...]:
        return tuple(record for record in self._records if not record.allowed)

    def assert_no_outside_access(self) -> None:
        outside = self.outside_boundary_records
        if outside:
            raise ExplorationBoundaryAccessError(
                "outside-boundary source access attempted: "
                f"{[record.to_dict() for record in outside]!r}"
            )

    def summary(self) -> dict[str, int]:
        return {
            "reads_total": len(self._records),
            "bar_reads": sum(record.kind == "bar" for record in self._records),
            "terminal_event_reads": sum(
                record.kind == "terminal_event" for record in self._records
            ),
            "outside_boundary_reads": len(self.outside_boundary_records),
        }

    def _record_or_refuse(
        self, kind: str, venue: str, symbol: str, session: date
    ) -> None:
        allowed = self._start <= session <= self._end
        record = AccessRecord(
            kind=kind,
            venue=venue,
            symbol=symbol,
            session=session,
            allowed=allowed,
        )
        self._records.append(record)
        if not allowed:
            raise ExplorationBoundaryAccessError(
                f"{kind} read outside exploration boundary: {session.isoformat()} "
                f"not in {self._start.isoformat()}..{self._end.isoformat()}"
            )


def bars_from_crypto_corpus_records(
    records: Iterable[Mapping[str, Any]],
) -> tuple[DailyBar, ...]:
    """Adapt already-authorized labeled-corpus records without data mutation.

    The caller is responsible for obtaining records through the corpus policy
    loader.  This adapter refuses intraday or non-UTC records and deliberately
    performs no interpolation, filtering, or write operation.
    """
    bars: list[DailyBar] = []
    required = {
        "venue",
        "symbol",
        "frequency",
        "bucket_timezone",
        "open_time_utc",
        "open",
        "high",
        "low",
        "close",
        "base_volume",
        "quote_volume",
    }
    for record in records:
        missing = required - set(record)
        if missing:
            raise CryptoStageBInputError(
                f"crypto corpus record lacks required fields: {sorted(missing)!r}"
            )
        raw_timestamp = record["open_time_utc"]
        if not isinstance(raw_timestamp, datetime) or raw_timestamp.tzinfo is None:
            raise CryptoStageBInputError(
                "open_time_utc must be a timezone-aware datetime"
            )
        utc_timestamp = raw_timestamp.astimezone(UTC)
        bars.append(
            DailyBar(
                venue=str(record["venue"]),
                symbol=str(record["symbol"]),
                session=utc_timestamp.date(),
                open=float(record["open"]),
                high=float(record["high"]),
                low=float(record["low"]),
                close=float(record["close"]),
                base_volume=float(record["base_volume"]),
                quote_volume=float(record["quote_volume"]),
                frequency=str(record["frequency"]),
                bucket_timezone=str(record["bucket_timezone"]),
            )
        )
    return tuple(bars)


def source_from_labeled_corpus(
    corpus: LabeledCorpus,
    *,
    exploration_start: date,
    exploration_end: date,
    terminal_events: Iterable[TerminalEvent] = (),
) -> InMemoryDailyBarSource:
    """Bind an already policy-checked, exact-window corpus extract to Stage-B.

    ``crypto_corpus.load_labeled_parquet*`` is the mandatory upstream reader;
    it validates file labels and refuses holdout paths before rows are decoded.
    This adapter requires the caller to supply an extract entirely inside the
    explicit Stage-B window.  It refuses, rather than silently filters, any
    out-of-window row so a future/holdout bar cannot be smuggled into memory.
    """
    if corpus.consumer_intent != "time_series":
        raise CryptoStageBInputError(
            "crypto Stage-B requires time_series corpus intent, never xsec"
        )
    if exploration_start > exploration_end:
        raise CryptoStageBInputError("exploration boundary start is after end")
    required = {
        "venue",
        "symbol",
        "frequency",
        "bucket_timezone",
        "open_time_utc",
        "open",
        "high",
        "low",
        "close",
        "base_volume",
        "quote_volume",
    }
    observed = set(corpus.table.column_names)
    missing = required - observed
    if missing:
        raise CryptoStageBInputError(
            f"labeled corpus table lacks required fields: {sorted(missing)!r}"
        )
    columns = {name: corpus.table.column(name).to_pylist() for name in sorted(required)}
    records = tuple(
        dict(zip(columns, values, strict=True))
        for values in zip(*(columns[name] for name in columns), strict=True)
    )
    bars = bars_from_crypto_corpus_records(records)
    if any(bar.venue != corpus.policy.venue for bar in bars):
        raise CryptoStageBInputError(
            "labeled corpus row venue does not match its verified file policy"
        )
    out_of_window = tuple(
        bar.session
        for bar in bars
        if not exploration_start <= bar.session <= exploration_end
    )
    if out_of_window:
        raise CryptoStageBInputError(
            "labeled corpus extract contains rows outside explicit exploration "
            f"window; first={out_of_window[0].isoformat()}"
        )
    return InMemoryDailyBarSource(bars, terminal_events=terminal_events)
