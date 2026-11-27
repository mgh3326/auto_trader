"""Refill the symbol of KR DART rows stored without one (hk 5623).

Rows collected before the corp-name registry resolution (hk 4591) can carry
``symbol IS NULL``. This module re-collects the listing for the affected days
through the existing DART fetch path, resolves each row with the same
``normalize_dart_disclosure_row`` rule, and writes only the ``symbol`` column of
rows that still lack one.

* ``first_received_at``, the raw payload, and every other stored value are not
  touched. Writes go through ``MarketEventsRepository`` only.
* A row that still does not resolve keeps ``symbol IS NULL``; its reason is
  counted in the report, not written back.
* Preview is the default. Nothing is written unless ``commit=True``.
* A repeat run finds no ``symbol IS NULL`` rows for the rows it already filled,
  so it changes nothing.

No broker, order, watch, or scheduler code is imported here.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from app.services.market_events.normalizers import normalize_dart_disclosure_row

RESOLVED = "resolved"
UNRESOLVED_OTHER = "unresolved_other"
RESOLUTION_REASONS = (
    "no_registry_match",
    "unlisted",
    "ambiguous_corp_name",
    "registry_unavailable",
)


class DartSymbolStore(Protocol):
    """Storage port. The database adapter delegates to MarketEventsRepository."""

    async def list_null_symbol_rows(
        self, *, from_date: date, to_date: date
    ) -> list[tuple[str, date]]: ...

    async def fill_symbol_if_null(self, *, rcept_no: str, symbol: str) -> int: ...

    async def commit(self) -> None: ...


FetchRows = Callable[[date], Awaitable[list[dict[str, Any]]]]
LoadRegistry = Callable[[], Awaitable[Mapping[str, list[dict[str, str]]] | None]]


@dataclass
class BackfillReport:
    mode: str
    from_date: date
    to_date: date
    candidate_rows: int = 0
    dates_scanned: int = 0
    # Count of rows per resolution outcome: ``resolved`` or one of
    # RESOLUTION_REASONS, plus UNRESOLVED_OTHER for a row that resolved to no
    # symbol without a recorded reason.
    reasons: dict[str, int] = field(default_factory=dict)
    # Rows that resolved and were written (commit) or would be written (preview).
    filled: int = 0
    would_fill: int = 0
    # Resolved, but a concurrent writer already set the symbol before our update.
    already_filled: int = 0
    not_in_listing: int = 0
    unparseable: int = 0
    date_errors: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.date_errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "from_date": self.from_date.isoformat(),
            "to_date": self.to_date.isoformat(),
            "candidate_rows": self.candidate_rows,
            "dates_scanned": self.dates_scanned,
            "reasons": dict(sorted(self.reasons.items())),
            "filled": self.filled,
            "would_fill": self.would_fill,
            "already_filled": self.already_filled,
            "not_in_listing": self.not_in_listing,
            "unparseable": self.unparseable,
            "date_errors": list(self.date_errors),
        }


def _bump(report: BackfillReport, reason: str) -> None:
    report.reasons[reason] = report.reasons.get(reason, 0) + 1


async def backfill_dart_symbols(
    store: DartSymbolStore,
    *,
    from_date: date,
    to_date: date,
    commit: bool,
    fetch_rows: FetchRows,
    load_corp_registry: LoadRegistry,
) -> BackfillReport:
    """Re-collect the affected days and fill the symbol of rows that now resolve."""
    if from_date > to_date:
        raise ValueError("from_date must be <= to_date")

    report = BackfillReport(
        mode="commit" if commit else "preview",
        from_date=from_date,
        to_date=to_date,
    )
    candidates = await store.list_null_symbol_rows(from_date=from_date, to_date=to_date)
    # One stored row per rcept_no; the same filing is never resolved twice.
    by_date: dict[date, dict[str, None]] = defaultdict(dict)
    seen: set[str] = set()
    for rcept_no, event_date in candidates:
        if rcept_no in seen:
            continue
        seen.add(rcept_no)
        by_date[event_date][rcept_no] = None
    report.candidate_rows = len(seen)
    if not seen:
        return report

    corp_registry: Mapping[str, list[dict[str, str]]] | None
    try:
        corp_registry = await load_corp_registry()
    except Exception:
        # Same policy as ingestion: the registry is enrichment. A failed read
        # leaves rows unresolved as ``registry_unavailable``, never guessed.
        corp_registry = None

    for event_date in sorted(by_date):
        report.dates_scanned += 1
        try:
            rows = await fetch_rows(event_date)
        except Exception as exc:
            report.date_errors.append(
                {"date": event_date.isoformat(), "error_class": type(exc).__name__}
            )
            continue

        listing = {
            str(row.get("rcept_no") or row.get("rcp_no") or "").strip(): row
            for row in rows
        }
        for rcept_no in by_date[event_date]:
            row = listing.get(rcept_no)
            if row is None:
                report.not_in_listing += 1
                continue
            try:
                event, _ = normalize_dart_disclosure_row(
                    row, corp_registry=corp_registry
                )
            except ValueError:
                report.unparseable += 1
                continue

            symbol = event.get("symbol")
            if not symbol:
                reason = (event.get("raw_payload_json") or {}).get(
                    "symbol_resolution_reason"
                )
                _bump(report, reason or UNRESOLVED_OTHER)
                continue

            _bump(report, RESOLVED)
            if not commit:
                report.would_fill += 1
                continue
            changed = await store.fill_symbol_if_null(rcept_no=rcept_no, symbol=symbol)
            if changed:
                report.filled += changed
            else:
                report.already_filled += 1

        if commit:
            await store.commit()

    return report


class RepositoryDartSymbolStore:
    """Database adapter: every read and write goes through MarketEventsRepository."""

    def __init__(self, db: Any) -> None:
        from app.services.market_events.repository import MarketEventsRepository

        self._db = db
        self._repo = MarketEventsRepository(db)

    async def list_null_symbol_rows(
        self, *, from_date: date, to_date: date
    ) -> list[tuple[str, date]]:
        return await self._repo.list_dart_null_symbol_rows(
            from_date=from_date, to_date=to_date
        )

    async def fill_symbol_if_null(self, *, rcept_no: str, symbol: str) -> int:
        return await self._repo.fill_dart_symbol_if_null(
            source_event_id=rcept_no, symbol=symbol
        )

    async def commit(self) -> None:
        await self._db.commit()
