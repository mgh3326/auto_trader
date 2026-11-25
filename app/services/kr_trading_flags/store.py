"""hk 6503 — the only writer and the reader for ``kr_symbol_trading_flags``.

Write rule, enforced in SQL so every caller gets it:

* A newer observation (``observed_at`` at or after the stored one) replaces the
  stored state for that (symbol, category).
* An older observation never replaces a newer one.
* An ``unmapped`` observation never replaces a mapped state. A code we cannot
  read must not un-exclude a symbol that a mapped source excluded.

Reads return what is stored. A missing (symbol, category) is unknown; callers
must not treat it as ``none``.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_trading_flags import KRSymbolTradingFlag
from app.services.kr_trading_flags import vocabulary as v

_CHUNK = 1000


@dataclass(frozen=True)
class FlagRecord:
    category: str
    state: str
    raw_code: str | None
    reason_code: str | None
    effective_date: date | None
    source: str
    observed_at: datetime


@dataclass(frozen=True)
class WriteStats:
    attempted: int
    written: int

    @property
    def guarded(self) -> int:
        """Observations the write guard kept out (older or unmapped over mapped)."""
        return self.attempted - self.written


def _dedupe_last(observations: Iterable[v.FlagObservation]) -> list[v.FlagObservation]:
    """Keep the last reading per (symbol, category).

    A single statement cannot touch the same row twice, and a source that lists a
    symbol twice has no defined winner, so the last reading in the source order
    is kept.
    """
    latest: dict[tuple[str, str], v.FlagObservation] = {}
    for obs in observations:
        latest[(obs.symbol, obs.category)] = obs
    return list(latest.values())


async def upsert_flag_observations(
    db: AsyncSession,
    observations: Iterable[v.FlagObservation],
    *,
    observed_at: datetime,
) -> WriteStats:
    """Apply the write guard to ``observations`` and report what was written."""
    rows = _dedupe_last(observations)
    for obs in rows:
        if obs.source not in v.SOURCES:
            raise ValueError(f"unknown flag source: {obs.source}")
        if not v.is_valid_state(obs.category, obs.state):
            raise ValueError(f"state {obs.state!r} is not valid for {obs.category}")
    if not rows:
        return WriteStats(attempted=0, written=0)

    table = KRSymbolTradingFlag.__table__
    written = 0
    for start in range(0, len(rows), _CHUNK):
        chunk = rows[start : start + _CHUNK]
        values = [
            {
                "symbol": obs.symbol,
                "category": obs.category,
                "state": obs.state,
                "raw_code": obs.raw_code or None,
                "reason_code": obs.reason_code,
                "effective_date": obs.effective_date,
                "source": obs.source,
                "observed_at": observed_at,
            }
            for obs in chunk
        ]
        stmt = pg_insert(table).values(values)
        excluded = stmt.excluded
        stmt = stmt.on_conflict_do_update(
            index_elements=[table.c.symbol, table.c.category],
            set_={
                "state": excluded.state,
                "raw_code": excluded.raw_code,
                "reason_code": excluded.reason_code,
                "effective_date": excluded.effective_date,
                "source": excluded.source,
                "observed_at": excluded.observed_at,
                "updated_at": func.now(),
            },
            where=and_(
                table.c.observed_at <= excluded.observed_at,
                or_(
                    excluded.state != v.STATE_UNMAPPED,
                    table.c.state == v.STATE_UNMAPPED,
                ),
            ),
        )
        result = await db.execute(stmt)
        written += int(result.rowcount or 0)
    await db.flush()
    return WriteStats(attempted=len(rows), written=written)


async def load_flag_records(
    db: AsyncSession,
    symbols: Iterable[str],
) -> dict[str, dict[str, FlagRecord]]:
    """Return ``{symbol: {category: FlagRecord}}`` for the stored rows only.

    A symbol or category with no row is absent from the result. That absence is
    the unknown state.
    """
    wanted = sorted({s for s in symbols if s})
    if not wanted:
        return {}
    result = await db.execute(
        select(KRSymbolTradingFlag).where(KRSymbolTradingFlag.symbol.in_(wanted))
    )
    out: dict[str, dict[str, FlagRecord]] = {}
    for row in result.scalars().all():
        out.setdefault(row.symbol, {})[row.category] = FlagRecord(
            category=row.category,
            state=row.state,
            raw_code=row.raw_code,
            reason_code=row.reason_code,
            effective_date=row.effective_date,
            source=row.source,
            observed_at=row.observed_at,
        )
    return out
