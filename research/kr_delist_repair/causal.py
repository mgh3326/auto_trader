"""Point-in-time convention: every row carries ``available_at``.

* A daily bar for session ``d`` is available at ``d 16:00 Asia/Seoul``: the
  regular session closes at 15:30 and the extra 30 minutes is a publication
  buffer.  A decision "at session d's close" therefore uses
  ``decision_time >= d 16:00 KST``.
* A terminal event (delisting) is available at the KRX notice filing time when
  a notice body was used, otherwise at ``delist_date 00:00 Asia/Seoul`` (the
  conservative choice: the KIND list row carries no announcement time).

``CausalTable.lookup`` and ``causal_join`` fail closed: asking for a row, or
for a column whose own availability column is later than the decision time,
raises ``AvailabilityViolation``.  Nothing is silently dropped or filled.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pyarrow as pa

from research.kr_delist_repair.model import AvailabilityViolation

KST = ZoneInfo("Asia/Seoul")
BAR_PUBLISH_TIME = time(16, 0)


def bar_available_at(session: date) -> datetime:
    return datetime.combine(session, BAR_PUBLISH_TIME, KST)


def _aware(value: datetime, what: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AvailabilityViolation(f"{what} must be timezone-aware, got {value!r}")
    return value


def assert_available(
    available_at: datetime | None, decision_time: datetime, what: str
) -> None:
    """Raise unless ``available_at`` is known and not after ``decision_time``."""
    _aware(decision_time, "decision_time")
    if available_at is None:
        raise AvailabilityViolation(f"{what}: availability unknown; refusing to use it")
    if _aware(available_at, what) > decision_time:
        raise AvailabilityViolation(
            f"{what}: available_at {available_at.isoformat()} is after "
            f"decision_time {decision_time.isoformat()}"
        )


TERMINAL_GATE = "terminal_available_at"
PANEL_GATES: dict[str, str] = {
    "is_terminal_session": TERMINAL_GATE,
    "terminal_outcome": TERMINAL_GATE,
    "payout_semantics": TERMINAL_GATE,
    "delist_date": TERMINAL_GATE,
    TERMINAL_GATE: TERMINAL_GATE,
}
"""Every terminal column of the export and the column that gates it."""


class CausalTable:
    """Keyed rows with a row-level ``available_at`` and optional per-column gates.

    ``column_gates`` maps a column to the column holding that column's own
    availability time (for example ``terminal_outcome`` →
    ``terminal_available_at``).
    """

    def __init__(
        self,
        table: pa.Table,
        key: Sequence[str],
        *,
        available_col: str = "available_at",
        column_gates: Mapping[str, str] | None = None,
    ) -> None:
        missing = [c for c in (*key, available_col) if c not in table.column_names]
        if missing:
            raise ValueError(f"CausalTable missing columns {missing}")
        self.key = tuple(key)
        self.available_col = available_col
        self.column_gates = dict(column_gates or {})
        ungated = [
            c
            for c in PANEL_GATES
            if c in table.column_names and self.column_gates.get(c) != PANEL_GATES[c]
        ]
        if ungated:
            raise ValueError(
                f"terminal columns {ungated} need their gate; use CausalTable.for_panel"
            )
        self._rows = table.to_pylist()
        self._index: dict[tuple[object, ...], int] = {}
        for i, row in enumerate(self._rows):
            k = tuple(row[c] for c in self.key)
            if k in self._index:
                raise ValueError(f"duplicate key {k}")
            self._index[k] = i

    @classmethod
    def for_panel(cls, table: pa.Table) -> CausalTable:
        """The export panel keyed by (symbol, session) with every terminal column gated."""
        return cls(table, ["symbol", "session"], column_gates=PANEL_GATES)

    def lookup(
        self,
        key: Sequence[object],
        decision_time: datetime,
        columns: Iterable[str] | None = None,
    ) -> dict[str, object]:
        k = tuple(key)
        if k not in self._index:
            raise KeyError(f"no row for {k}")
        row = self._rows[self._index[k]]
        assert_available(row[self.available_col], decision_time, f"row {k}")
        wanted = list(columns) if columns is not None else list(row)
        out: dict[str, object] = {}
        for col in wanted:
            gate = self.column_gates.get(col)
            # A gated column holding a value (anything but None/False) is only
            # readable once its gate time is known and has passed.
            if gate is not None and row[col] is not None and row[col] is not False:
                assert_available(row[gate], decision_time, f"column {col} of row {k}")
            out[col] = row[col]
        return out


def causal_join(
    left: Iterable[Mapping[str, object]],
    right: CausalTable,
    *,
    left_key: Sequence[str],
    decision_col: str,
    columns: Sequence[str],
) -> list[dict[str, object]]:
    """Attach ``columns`` of ``right`` to each left row; raise on any violation."""
    out: list[dict[str, object]] = []
    for row in left:
        decision = row[decision_col]
        if not isinstance(decision, datetime):
            raise AvailabilityViolation(
                f"{decision_col} must be a datetime, got {decision!r}"
            )
        found = right.lookup([row[c] for c in left_key], decision, columns)
        out.append({**row, **found})
    return out
