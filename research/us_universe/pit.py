"""Point-in-time (PIT) listing intervals built from dated directory observations.

Model
-----
Each series (``nasdaqlisted``/``otherlisted``) is a sequence of complete,
dated observations ``o_1 < o_2 < ... < o_n``. For every symbol, a *listing
interval* is a maximal run of consecutive observations that contain it:

* ``listed_from``  — date of the first observation in the run (inclusive);
* ``listed_to``    — date of the last observation in the run (inclusive), or
  empty when the run reaches ``o_n`` (still listed: ``status=active``);
* ``delisted_by``  — date of the first observation *after* the run, i.e. the
  delisting happened in ``(listed_to, delisted_by]``;
* ``listed_after`` — date of the last observation *before* the run, i.e. the
  listing happened in ``(listed_after, listed_from]``; empty when censored;
* ``listed_from_censored`` — the run starts at ``o_1``, so the true listing
  date may be earlier than the archive reaches.

Dates are evidence, not exchange notices: between two archived observations
the true event date is unknown, and that uncertainty is kept in the row
(``listed_from``/``delisted_by`` bracket it) rather than guessed.

``universe_as_of(t)`` returns symbols with ``listed_from <= t`` and
(``delisted_by`` empty or ``t < delisted_by``):

* a symbol first observed after ``t`` is excluded (no look-ahead on listing);
* a symbol is kept until the first observation that no longer shows it. Inside
  the bracket ``(listed_to, delisted_by)`` an observer at ``t`` has the same
  evidence for a symbol about to delist as for one that survives ("last seen
  listed"), so treating them differently would use the later observation —
  look-ahead that removes exactly the names about to delist (survivorship).

``end="listed_to"`` selects the narrower evidence-only rule
(``t <= listed_to``) for callers that want it, with that bias stated.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date

from .symdir import Observation

CSV_COLUMNS = (
    "symbol",
    "listed_from",
    "listed_to",
    "status",
    "status_source",
    "delisted_by",
    "listed_after",
    "listed_from_censored",
    "series",
    "exchange",
    "security_name",
    "etf",
    "likely_common_stock",
    "observations",
)


@dataclass(frozen=True)
class PITRow:
    symbol: str
    listed_from: date
    listed_to: date | None  # None ⇔ active
    status: str  # active | inactive
    status_source: str  # "<series>@<date>" — the observation that decides status
    delisted_by: date | None
    listed_after: date | None  # None ⇔ listed_from_censored
    listed_from_censored: bool
    series: str
    exchange: str
    security_name: str  # as of the last observation in the run
    etf: bool | None
    likely_common_stock: bool
    observations: int  # observations inside the run

    def listed_at(self, t: date, *, end: str = "delisted_by") -> bool:
        if t < self.listed_from:
            return False
        if end == "delisted_by":
            return self.delisted_by is None or t < self.delisted_by
        if end == "listed_to":
            return self.listed_to is None or t <= self.listed_to
        raise ValueError(f"unknown end rule {end!r}")


def _rows_digest(obs: Observation) -> str:
    return hashlib.sha256(repr(obs.rows).encode("utf-8")).hexdigest()


def _dedupe_by_day(observations: Iterable[Observation]) -> dict[str, list[Observation]]:
    """Group per series, one observation per ``as_of`` day.

    Two captures of the same day: the later file creation time wins; then the
    larger file; then the larger content digest. Every key is totally ordered,
    so the choice depends only on the set of inputs, never on their order.
    """

    def rank(o: Observation) -> tuple[str, int, str]:
        return (o.created_at, len(o.rows), _rows_digest(o))

    per_series: dict[str, dict[date, Observation]] = {}
    for obs in observations:
        day = per_series.setdefault(obs.series, {})
        prev = day.get(obs.as_of)
        if prev is None or rank(obs) > rank(prev):
            day[obs.as_of] = obs
    return {s: [d[k] for k in sorted(d)] for s, d in sorted(per_series.items())}


def build_pit_rows(observations: Iterable[Observation]) -> list[PITRow]:
    rows: list[PITRow] = []
    for series, seq in _dedupe_by_day(observations).items():
        n = len(seq)
        present = [{r.symbol: r for r in obs.rows} for obs in seq]
        symbols = sorted(set().union(*present)) if present else []
        for symbol in symbols:
            i = 0
            while i < n:
                if symbol not in present[i]:
                    i += 1
                    continue
                start = i
                while i + 1 < n and symbol in present[i + 1]:
                    i += 1
                end = i
                last = present[end][symbol]
                active = end == n - 1
                delisted_by = None if active else seq[end + 1].as_of
                status_date = seq[end].as_of if active else delisted_by
                rows.append(
                    PITRow(
                        symbol=symbol,
                        listed_from=seq[start].as_of,
                        listed_to=None if active else seq[end].as_of,
                        status="active" if active else "inactive",
                        status_source=f"{series}@{status_date.isoformat()}",
                        delisted_by=delisted_by,
                        listed_after=None if start == 0 else seq[start - 1].as_of,
                        listed_from_censored=start == 0,
                        series=series,
                        exchange=last.exchange,
                        security_name=last.security_name,
                        etf=last.etf,
                        likely_common_stock=last.likely_common_stock,
                        observations=end - start + 1,
                    )
                )
                i += 1
    rows.sort(key=lambda r: (r.symbol, r.listed_from, r.series))
    return rows


def universe_as_of(
    rows: Sequence[PITRow], t: date, *, end: str = "delisted_by"
) -> list[str]:
    """Symbols listed on ``t`` (sorted, unique across series)."""
    return sorted({r.symbol for r in rows if r.listed_at(t, end=end)})


def _fmt(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def rows_to_csv(rows: Sequence[PITRow]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for r in rows:
        writer.writerow([_fmt(getattr(r, c)) for c in CSV_COLUMNS])
    return buf.getvalue().encode("utf-8")


def _parse_bool(v: str) -> bool | None:
    return {"true": True, "false": False, "": None}[v]


def _parse_date(v: str) -> date | None:
    return date.fromisoformat(v) if v else None


def rows_from_csv(data: bytes) -> list[PITRow]:
    reader = csv.DictReader(io.StringIO(data.decode("utf-8")))
    if tuple(reader.fieldnames or ()) != CSV_COLUMNS:
        raise ValueError(f"unexpected PIT columns: {reader.fieldnames}")
    out = []
    for rec in reader:
        listed_from = _parse_date(rec["listed_from"])
        if listed_from is None:
            raise ValueError(f"{rec['symbol']}: empty listed_from")
        out.append(
            PITRow(
                symbol=rec["symbol"],
                listed_from=listed_from,
                listed_to=_parse_date(rec["listed_to"]),
                status=rec["status"],
                status_source=rec["status_source"],
                delisted_by=_parse_date(rec["delisted_by"]),
                listed_after=_parse_date(rec["listed_after"]),
                listed_from_censored=bool(_parse_bool(rec["listed_from_censored"])),
                series=rec["series"],
                exchange=rec["exchange"],
                security_name=rec["security_name"],
                etf=_parse_bool(rec["etf"]),
                likely_common_stock=bool(_parse_bool(rec["likely_common_stock"])),
                observations=int(rec["observations"]),
            )
        )
    return out


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class InactiveShare:
    scope: str
    symbols: int
    inactive: int

    @property
    def share(self) -> float:
        return self.inactive / self.symbols if self.symbols else 0.0

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.scope,
            "symbols": self.symbols,
            "inactive": self.inactive,
            "inactive_share": round(self.share, 6),
        }


def inactive_shares(
    rows: Sequence[PITRow], *, window_start: date, window_end: date
) -> list[InactiveShare]:
    """Symbol-level inactive share. A symbol is inactive iff none of its
    intervals is active (a NYSE→Nasdaq transfer is one active symbol)."""

    def share(scope: str, selected: Iterable[PITRow]) -> InactiveShare:
        by_symbol: dict[str, bool] = {}
        for r in selected:
            by_symbol[r.symbol] = by_symbol.get(r.symbol, False) or r.status == "active"
        return InactiveShare(
            scope=scope,
            symbols=len(by_symbol),
            inactive=sum(1 for active in by_symbol.values() if not active),
        )

    def overlaps(r: PITRow) -> bool:  # same rule as ``listed_at``
        return r.listed_from <= window_end and (
            r.delisted_by is None or r.delisted_by > window_start
        )

    common = [r for r in rows if r.likely_common_stock]
    tag = f"{window_start.isoformat()}..{window_end.isoformat()}"
    return [
        share("all_symbols_all_time", rows),
        share("likely_common_stock_all_time", common),
        share(f"all_symbols_listed_in_{tag}", [r for r in rows if overlaps(r)]),
        share(
            f"likely_common_stock_listed_in_{tag}",
            [r for r in common if overlaps(r)],
        ),
    ]
