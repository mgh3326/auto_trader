"""Read persisted support-proximity snapshots (ROB-976).

This module is deliberately a pure database read.  Price, support level, and
distance were calculated from one completed OHLCV frame by the snapshot builder;
recomputing any of them here would reintroduce look-ahead and mixed timestamps.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.services.invest_screener_snapshots.support_proximity_policy import (
    DEFAULT_CANDIDATE_POOL_LIMIT,
    DEFAULT_MIN_MARKET_CAP_KRW,
    DEFAULT_MIN_TURNOVER_KRW,
)
from app.services.market_valuation_snapshots.normalized_market_cap import (
    KR_NORMALIZED_MARKET_CAP_SOURCE,
)

if TYPE_CHECKING:
    from app.services.invest_view_model.screener_service import _SnapshotLoadResult

logger = logging.getLogger(__name__)


async def load_support_proximity_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 30,
    min_market_cap: float | Decimal | None = None,
    min_turnover: float | Decimal | None = None,
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT,
    now: Callable[[], dt.datetime] | None = None,
) -> _SnapshotLoadResult | None:
    """Return stored rows ordered by support distance, with honest freshness.

    ``candidate_pool_limit`` remains in the internal signature for compatibility
    with the R1 preview caller, but has no read-time effect: bounding belongs to
    the builder, never to a query-time calculation fan-out.
    """

    del candidate_pool_limit
    if session is None or market != "kr":
        return None

    from app.models.symbol_sectors import SymbolSector
    from app.services.invest_screener_snapshots.freshness import (
        classify_state,
        expected_baseline_date,
    )
    from app.services.invest_screener_snapshots.partition_health import (
        HealthyPartition,
    )
    from app.services.invest_view_model.screener_service import (
        _is_toss_common_stock_row,
        _partition_degradation,
        _SnapshotLoadResult,
    )

    now_fn = now or (lambda: dt.datetime.now(dt.UTC))
    moment = now_fn()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)

    try:
        partition_date = (
            await session.execute(
                sa.select(sa.func.max(InvestScreenerSnapshot.snapshot_date)).where(
                    InvestScreenerSnapshot.market == "kr",
                    InvestScreenerSnapshot.support_computed_at.is_not(None),
                )
            )
        ).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - read path fails closed to missing
        logger.warning(
            "support_proximity partition lookup failed: %s", exc, exc_info=True
        )
        return None
    if partition_date is None:
        return None

    try:
        metadata = (
            await session.execute(
                sa.select(
                    sa.func.count().label("row_count"),
                    sa.func.max(InvestScreenerSnapshot.support_computed_at).label(
                        "computed_at"
                    ),
                ).where(
                    InvestScreenerSnapshot.market == "kr",
                    InvestScreenerSnapshot.snapshot_date == partition_date,
                    InvestScreenerSnapshot.support_computed_at.is_not(None),
                )
            )
        ).one()
    except Exception as exc:  # noqa: BLE001 - fail closed; never live-fall back
        logger.warning(
            "support_proximity metadata lookup failed: %s", exc, exc_info=True
        )
        return None
    row_count = int(metadata.row_count or 0)
    partition_computed_at = metadata.computed_at
    partition = HealthyPartition(
        partition_date=partition_date,
        row_count=row_count,
        coverage_ratio=1.0,
        is_fallback=False,
        healthy=True,
    )

    market_cap_floor = (
        Decimal(str(min_market_cap))
        if min_market_cap is not None
        else DEFAULT_MIN_MARKET_CAP_KRW
    )
    turnover_floor = (
        Decimal(str(min_turnover))
        if min_turnover is not None
        else DEFAULT_MIN_TURNOVER_KRW
    )

    try:
        result = await session.execute(
            sa.select(
                InvestScreenerSnapshot.symbol,
                InvestScreenerSnapshot.latest_close,
                InvestScreenerSnapshot.prev_close,
                InvestScreenerSnapshot.change_rate,
                InvestScreenerSnapshot.change_amount,
                InvestScreenerSnapshot.daily_volume,
                InvestScreenerSnapshot.daily_turnover,
                InvestScreenerSnapshot.market_cap,
                InvestScreenerSnapshot.market_cap_source,
                InvestScreenerSnapshot.market_cap_snapshot_date,
                InvestScreenerSnapshot.support_price,
                InvestScreenerSnapshot.support_kind,
                InvestScreenerSnapshot.support_strength,
                InvestScreenerSnapshot.dist_to_support_pct,
                InvestScreenerSnapshot.closes_window,
                InvestScreenerSnapshot.support_computed_at,
                KRSymbolUniverse.name,
                KRSymbolUniverse.security_type,
                KRSymbolUniverse.is_common_share,
                KRSymbolUniverse.krx_trading_suspended,
                KRSymbolUniverse.nxt_trading_suspended,
                SymbolSector.name_kr.label("sector_name_kr"),
                SymbolSector.name_en.label("sector_name_en"),
            )
            .join(
                KRSymbolUniverse,
                KRSymbolUniverse.symbol == InvestScreenerSnapshot.symbol,
            )
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(
                InvestScreenerSnapshot.market == "kr",
                InvestScreenerSnapshot.snapshot_date == partition_date,
                InvestScreenerSnapshot.dist_to_support_pct.is_not(None),
                InvestScreenerSnapshot.support_price.is_not(None),
                InvestScreenerSnapshot.support_computed_at.is_not(None),
                InvestScreenerSnapshot.market_cap.is_not(None),
                InvestScreenerSnapshot.market_cap_source
                == KR_NORMALIZED_MARKET_CAP_SOURCE,
                InvestScreenerSnapshot.market_cap >= market_cap_floor,
                InvestScreenerSnapshot.daily_turnover.is_not(None),
                InvestScreenerSnapshot.daily_turnover >= turnover_floor,
                KRSymbolUniverse.is_active.is_(True),
            )
            .order_by(
                InvestScreenerSnapshot.dist_to_support_pct.asc(),
                InvestScreenerSnapshot.symbol.asc(),
            )
        )
        snapshot_rows = list(result.mappings().all())
    except Exception as exc:  # noqa: BLE001 - loader reports missing, never live-falls back
        logger.warning(
            "support_proximity snapshot query failed: %s", exc, exc_info=True
        )
        return None

    baseline_date = expected_baseline_date("kr", now=moment)
    rows: list[dict[str, Any]] = []
    for snapshot in snapshot_rows:
        if not _is_toss_common_stock_row(
            symbol=snapshot["symbol"],
            name=snapshot["name"],
            security_type=snapshot["security_type"],
            is_common_share=snapshot["is_common_share"],
            trading_suspended=(
                snapshot["krx_trading_suspended"] or snapshot["nxt_trading_suspended"]
            ),
        ):
            continue
        computed_at = snapshot["support_computed_at"]
        state = classify_state(
            snapshot_date=partition_date,
            computed_at=computed_at,
            closes_window_len=len(snapshot["closes_window"] or []),
            today_trading_date_value=baseline_date,
            now=moment,
        )
        rows.append(
            {
                "symbol": snapshot["symbol"],
                "market": "kr",
                "name": snapshot["name"],
                "sector": (snapshot["sector_name_kr"] or snapshot["sector_name_en"]),
                "close": float(snapshot["latest_close"]),
                "latest_close": float(snapshot["latest_close"]),
                "prev_close": (
                    float(snapshot["prev_close"])
                    if snapshot["prev_close"] is not None
                    else None
                ),
                "change_rate": (
                    float(snapshot["change_rate"])
                    if snapshot["change_rate"] is not None
                    else None
                ),
                "change_amount": (
                    float(snapshot["change_amount"])
                    if snapshot["change_amount"] is not None
                    else None
                ),
                "volume": snapshot["daily_volume"],
                "turnover": float(snapshot["daily_turnover"]),
                "market_cap": float(snapshot["market_cap"]),
                "_market_cap_source": "primary",
                "market_cap_snapshot_source": snapshot["market_cap_source"],
                "market_cap_snapshot_date": snapshot["market_cap_snapshot_date"],
                "dist_to_support_pct": float(snapshot["dist_to_support_pct"]),
                "support_price": float(snapshot["support_price"]),
                "support_kind": snapshot["support_kind"],
                "support_strength": snapshot["support_strength"],
                "snapshot_date": partition_date,
                "computed_at": computed_at,
                "_screener_snapshot_state": state,
            }
        )
        if len(rows) >= max(1, limit):
            break

    reason, coverage_label = _partition_degradation(partition, rows_empty=not rows)
    return _SnapshotLoadResult(
        rows=rows,
        partition_date=partition_date,
        partition_computed_at=partition_computed_at,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
