"""PIT universe construction from membership snapshots only.

Rules:
* Universe at session ``t`` is built **only** from membership rows with
  ``session_date <= t``.
* Live / operational DB universe flags are **forbidden** — this module
  never imports app models or services; membership parquet is the only
  membership source.
* Delisted names surface via ``status == "delisted"`` and are excluded from
  the investable set; open positions must be terminalized explicitly
  (see ``terminal_events``), never silently dropped.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Literal

import pyarrow as pa
from holdout_guard import assert_date_not_holdout
from windows import parse_iso_date

__all__ = [
    "MembershipRow",
    "UniverseSnapshot",
    "membership_rows_from_table",
    "universe_at",
]

MembershipStatus = Literal["listed", "delisted", "suspended"]


@dataclass(frozen=True)
class MembershipRow:
    symbol: str
    session_date: date
    market: str
    member: bool
    status: MembershipStatus


@dataclass(frozen=True)
class UniverseSnapshot:
    """Investable symbols as of a session (PIT)."""

    session_date: date
    symbols: frozenset[str]
    # Symbols whose latest status as-of session is delisted (for terminalization).
    delisted_as_of: frozenset[str]


def membership_rows_from_table(table: pa.Table) -> list[MembershipRow]:
    rows: list[MembershipRow] = []
    data = table.to_pydict()
    n = table.num_rows
    for i in range(n):
        status = data["status"][i]
        if status not in ("listed", "delisted", "suspended"):
            raise ValueError(f"invalid membership status {status!r}")
        session = parse_iso_date(data["session_date"][i])
        assert_date_not_holdout(session)
        rows.append(
            MembershipRow(
                symbol=str(data["symbol"][i]),
                session_date=session,
                market=str(data["market"][i]),
                member=bool(data["member"][i]),
                status=status,  # type: ignore[arg-type]
            )
        )
    return rows


def universe_at(
    membership: Iterable[MembershipRow],
    session: date | str,
) -> UniverseSnapshot:
    """Build the investable universe at ``session`` from membership only.

    For each symbol, take the latest membership row with ``session_date <= session``.
    Investable iff that row has ``member is True`` and ``status == "listed"``.
    """
    t = assert_date_not_holdout(session)
    latest: dict[str, MembershipRow] = {}
    for row in membership:
        if row.session_date > t:
            continue  # PIT: never use future membership
        prev = latest.get(row.symbol)
        if prev is None or row.session_date >= prev.session_date:
            latest[row.symbol] = row

    symbols: set[str] = set()
    delisted: set[str] = set()
    for symbol, row in latest.items():
        if row.status == "delisted":
            delisted.add(symbol)
            continue
        if row.member and row.status == "listed":
            symbols.add(symbol)
    return UniverseSnapshot(
        session_date=t,
        symbols=frozenset(symbols),
        delisted_as_of=frozenset(delisted),
    )
