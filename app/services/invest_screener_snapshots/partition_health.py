"""ROB-426 PR2a — read-path latest-healthy-partition selection.

A /invest/screener loader must not let a thin smoke partition (e.g. 20 rows of a
~3,900 active universe) shadow a healthy older partition (~3,800 rows). This
module resolves the most recent partition whose TOTAL row count meets a coverage
bar (a fraction of the active universe), falling back to older partitions
(bounded scan-back), and never reduces availability: when no scanned partition is
healthy it returns the newest as a degraded last resort, and returns None only
when the table has no partitions for the market.

"Coverage" = total rows in a partition (the scored universe), NOT the number of
preset qualifiers — qualifier filtering happens downstream, unchanged.

Constants are locked here; changing them is a separate telemetry-backed PR
(mirrors invest_screener_snapshots/guards.py convention).
"""

from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.invest_screener_snapshots.freshness import DataState

logger = logging.getLogger(__name__)

#: A partition is healthy when its total row count is at least this fraction of
#: the active universe. Distinct from the 2b commit-guard floors. Change = PR.
_MIN_HEALTHY_COVERAGE_RATIO = 0.50
#: Bound the scan-back so a degenerate table cannot trigger an unbounded walk.
_MAX_PARTITION_SCAN_BACK = 10

_DEGRADED_FLOOR: DataState = "stale"
_KEEP_STATES: frozenset[DataState] = frozenset({"missing", "fallback", "stale"})


@dataclass(frozen=True)
class HealthyPartition:
    partition_date: dt.date
    row_count: int
    coverage_ratio: float
    is_fallback: bool  # older than the newest partition
    healthy: bool  # row_count met the coverage floor


def served_partition_degraded(hp: HealthyPartition | None) -> bool:
    """Whether a resolved partition should be treated as degraded for freshness.

    ROB-440: a HEALTHY fallback (``is_fallback=True`` but ``healthy=True``) is NOT
    degraded — resolve_healthy_partition correctly served the latest *healthy*
    partition because a thinner raw-latest partition (e.g. a partial weekend build:
    a 388-row 2026-06-06 partition shadowing the healthy 4,926-row 2026-06-05) was
    skipped. Whether the served partition is stale vs the expected baseline is a
    separate date check in the caller (``val_date == today_market_date``). Only a
    served partition that failed the coverage floor (``healthy=False`` — no healthy
    partition existed, thinnest served as last resort) is genuinely degraded.
    """
    return bool(hp is not None and not hp.healthy)


def cap_degraded(state: DataState) -> DataState:
    """Never claim better than ``stale`` for a degraded partition.

    ``fresh``/``partial`` -> ``stale``; ``missing``/``fallback``/``stale`` kept.
    """
    return state if state in _KEEP_STATES else _DEGRADED_FLOOR


async def active_universe_count(session: AsyncSession, *, market: str) -> int:
    """Count active symbols for the market (the coverage denominator)."""
    try:
        if market == "kr":
            from app.models.kr_symbol_universe import KRSymbolUniverse

            stmt = (
                sa.select(sa.func.count())
                .select_from(KRSymbolUniverse)
                .where(KRSymbolUniverse.is_active.is_(True))
            )
        else:
            from app.models.us_symbol_universe import USSymbolUniverse

            # ROB-440: US screener snapshots (valuation/fundamentals/OHLCV) are built
            # over the COMMON-STOCK universe (is_common_stock), not the full active set
            # (~12.4k incl ETFs/preferreds/warrants we intentionally skip). Use the
            # common-stock count as the coverage denominator so a complete common-stock
            # build (~5.1k) isn't mislabeled "below floor" (~41% of the full universe →
            # partition flagged degraded → cap_degraded → spurious stale/"준비중").
            stmt = (
                sa.select(sa.func.count())
                .select_from(USSymbolUniverse)
                .where(
                    USSymbolUniverse.is_active.is_(True),
                    USSymbolUniverse.is_common_stock.is_(True),
                )
            )
        return int((await session.execute(stmt)).scalar() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "active_universe_count failed; falling back to 0: %s",
            exc,
            exc_info=True,
        )
        return 0


async def _partition_row_count(
    session: AsyncSession,
    *,
    model: Any,
    market_col: Any,
    market: str,
    date_col: Any,
    partition_date: dt.date,
) -> int:
    return int(
        (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(model)
                .where(market_col == market, date_col == partition_date)
            )
        ).scalar()
        or 0
    )


async def resolve_healthy_partition(
    session: AsyncSession,
    *,
    model: Any,
    date_col: Any,
    market_col: Any,
    market: str,
    universe_count: int | None = None,
    min_ratio: float = _MIN_HEALTHY_COVERAGE_RATIO,
    max_scan_back: int = _MAX_PARTITION_SCAN_BACK,
) -> HealthyPartition | None:
    """Return the partition to serve (see module docstring).

    None only when the table has no partitions for the market. Fail-open: on any
    query error, falls back to a plain max(date_col) treated as healthy.
    """
    try:
        dates = [
            d
            for (d,) in (
                await session.execute(
                    sa.select(date_col)
                    .where(market_col == market)
                    .distinct()
                    .order_by(date_col.desc())
                    .limit(max_scan_back)
                )
            ).all()
        ]
        if not dates:
            return None
        newest = dates[0]

        if universe_count is None:
            universe_count = await active_universe_count(session, market=market)
        if universe_count <= 0:
            return HealthyPartition(
                partition_date=newest,
                row_count=0,
                coverage_ratio=0.0,
                is_fallback=False,
                healthy=True,
            )

        floor = math.ceil(universe_count * min_ratio)
        for d in dates:
            count = await _partition_row_count(
                session,
                model=model,
                market_col=market_col,
                market=market,
                date_col=date_col,
                partition_date=d,
            )
            if count >= floor:
                return HealthyPartition(
                    partition_date=d,
                    row_count=count,
                    coverage_ratio=count / universe_count,
                    is_fallback=(d != newest),
                    healthy=True,
                )

        newest_count = await _partition_row_count(
            session,
            model=model,
            market_col=market_col,
            market=market,
            date_col=date_col,
            partition_date=newest,
        )
        return HealthyPartition(
            partition_date=newest,
            row_count=newest_count,
            coverage_ratio=newest_count / universe_count,
            is_fallback=False,
            healthy=False,
        )
    except Exception as exc:  # noqa: BLE001  — fail-open, never reduce availability
        logger.warning(
            "resolve_healthy_partition failed; falling back to max(): %s",
            exc,
            exc_info=True,
        )
        try:
            newest = (
                await session.execute(
                    sa.select(sa.func.max(date_col)).where(market_col == market)
                )
            ).scalar_one_or_none()
        except Exception:  # noqa: BLE001
            return None
        if newest is None:
            return None
        return HealthyPartition(
            partition_date=newest,
            row_count=0,
            coverage_ratio=0.0,
            is_fallback=False,
            healthy=True,
        )
