"""Read-only loader for the 지지선 근접 (support_proximity) preset (ROB-976).

Ranks the quality-filtered KR universe by distance to the nearest support
level. The clustering itself (Fibonacci / volume-profile / Bollinger) is
reused from ``get_support_resistance_impl`` — never reimplemented.

``invest_screener_snapshots.closes_window`` only carries closing prices (no
high/low/volume history), so it cannot feed the fib/volume-profile clustering
on its own. This loader instead uses the latest invest_screener_snapshots
partition ONLY to resolve price + quality-filter inputs (market cap /
turnover / common-stock), then bounds a LIVE ``get_support_resistance_impl``
fan-out to the top-N blue-chip candidates that pass those filters. No new
table/column is introduced and no write path exists here —
``InvestScreenerSnapshotsRepository.upsert`` remains the sole writer.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.invest_view_model.screener_service import _SnapshotLoadResult

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

# ROB-976: internal quality-filter floors. These bound the live
# get_support_resistance fan-out to blue-chip-ish names — they are NOT the
# caller-tunable knob (screen_stocks_snapshot's generic min_market_cap /
# min_market_cap_eok params narrow further at the result layer, same as every
# other preset).
DEFAULT_MIN_MARKET_CAP_KRW = 300_000_000_000.0  # 3천억원
DEFAULT_MIN_TURNOVER_KRW = 1_000_000_000.0  # 10억원 (daily_volume * latest_close)
DEFAULT_CANDIDATE_POOL_LIMIT = 60
_LIVE_CHECK_CONCURRENCY = 6


async def load_support_proximity_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 30,
    min_market_cap: float | None = None,
    min_turnover: float | None = None,
    candidate_pool_limit: int = DEFAULT_CANDIDATE_POOL_LIMIT,
) -> _SnapshotLoadResult | None:
    """Return support_proximity rows ordered by dist_to_support_pct ascending.

    None  -> no snapshot partition exists; caller should report dataState=missing.
    []    -> a partition exists but nothing passed the quality filters, or every
             checked candidate had no support below its current price (fail-closed,
             never fabricated).
    Rows  -> ordered by dist_to_support_pct asc (closest support first).
    """
    if session is None or market != "kr":
        return None

    from app.services.invest_screener_snapshots.partition_health import (
        active_universe_count,
        cap_degraded,
        resolve_healthy_partition,
    )
    from app.services.invest_view_model.screener_service import (
        _is_kr_toss_common_stock,
        _partition_degradation,
        _SnapshotLoadResult,
    )

    universe_count = await active_universe_count(session, market="kr")
    price_hp = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    if price_hp is None:
        return None
    price_date = price_hp.partition_date

    floor_market_cap = (
        float(min_market_cap)
        if min_market_cap is not None
        else DEFAULT_MIN_MARKET_CAP_KRW
    )
    floor_turnover = (
        float(min_turnover) if min_turnover is not None else DEFAULT_MIN_TURNOVER_KRW
    )

    try:
        price_result = await session.execute(
            sa.select(
                InvestScreenerSnapshot.symbol,
                InvestScreenerSnapshot.latest_close,
                InvestScreenerSnapshot.prev_close,
                InvestScreenerSnapshot.change_rate,
                InvestScreenerSnapshot.change_amount,
                InvestScreenerSnapshot.daily_volume,
            ).where(
                InvestScreenerSnapshot.market == "kr",
                InvestScreenerSnapshot.snapshot_date == price_date,
                InvestScreenerSnapshot.latest_close.is_not(None),
            )
        )
        price_rows = list(price_result.mappings().all())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "support_proximity: candidate query failed: %s", exc, exc_info=True
        )
        return None

    if not price_rows:
        reason, coverage_label = _partition_degradation(price_hp, rows_empty=True)
        return _SnapshotLoadResult(
            rows=[],
            partition_date=price_date,
            partition_computed_at=None,
            degradation_reason=reason,
            coverage_label=coverage_label,
        )

    symbols = [r["symbol"] for r in price_rows]

    from app.models.symbol_sectors import SymbolSector

    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    try:
        names = await session.execute(
            sa.select(
                KRSymbolUniverse.symbol,
                KRSymbolUniverse.name,
                SymbolSector.name_kr.label("sector_name_kr"),
                SymbolSector.name_en.label("sector_name_en"),
            )
            .outerjoin(SymbolSector, KRSymbolUniverse.sector_id == SymbolSector.id)
            .where(
                KRSymbolUniverse.symbol.in_(symbols),
                KRSymbolUniverse.is_active.is_(True),
            )
        )
        _name_rows = names.all()
        name_map = {row.symbol: row.name for row in _name_rows}
        sector_map = {
            row.symbol: label
            for row in _name_rows
            if (
                label := (
                    getattr(row, "sector_name_kr", None)
                    or getattr(row, "sector_name_en", None)
                )
            )
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("support_proximity: name lookup failed: %s", exc, exc_info=True)

    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition as _resolve_val_hp,
    )
    from app.services.market_valuation_snapshots.repository import metric_rich_filter

    val_hp = await _resolve_val_hp(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="kr",
        universe_count=universe_count,
        row_filter=metric_rich_filter(),  # ROB-551: skip toss-only partitions
    )
    if val_hp is not None:
        market_cap_source = "fallback" if val_hp.is_fallback else "primary"
        try:
            _mc = await session.execute(
                sa.select(
                    MarketValuationSnapshot.symbol,
                    MarketValuationSnapshot.market_cap,
                ).where(
                    MarketValuationSnapshot.market == "kr",
                    MarketValuationSnapshot.snapshot_date == val_hp.partition_date,
                    MarketValuationSnapshot.symbol.in_(symbols),
                )
            )
            market_cap_map = {
                r.symbol: float(r.market_cap)
                for r in _mc.all()
                if r.market_cap is not None
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "support_proximity: market_cap lookup failed: %s", exc, exc_info=True
            )

    candidates: list[dict[str, Any]] = []
    for r in price_rows:
        sym = r["symbol"]
        name = name_map.get(sym)
        if not _is_kr_toss_common_stock(sym, name):
            continue
        market_cap = market_cap_map.get(sym)
        if market_cap is None or market_cap < floor_market_cap:
            continue
        close = float(r["latest_close"])
        volume = r["daily_volume"]
        turnover = close * float(volume) if volume is not None else None
        if turnover is None or turnover < floor_turnover:
            continue
        candidates.append(
            {
                "symbol": sym,
                "name": name,
                "sector": sector_map.get(sym),
                "latest_close": close,
                "prev_close": (
                    float(r["prev_close"]) if r["prev_close"] is not None else None
                ),
                "change_rate": (
                    float(r["change_rate"]) if r["change_rate"] is not None else None
                ),
                "change_amount": (
                    float(r["change_amount"])
                    if r["change_amount"] is not None
                    else None
                ),
                "daily_volume": volume,
                "market_cap": market_cap,
            }
        )

    # ROB-976: bound the live get_support_resistance fan-out to the top-N by
    # market cap (blue-chip preference) — never scan the full filtered set live.
    candidates.sort(key=lambda c: c["market_cap"], reverse=True)
    bounded_candidates = candidates[: max(candidate_pool_limit, limit)]

    row_state = (
        cap_degraded("fresh")
        if not price_hp.healthy or price_hp.is_fallback
        else "fresh"
    )

    if not bounded_candidates:
        reason, coverage_label = _partition_degradation(price_hp, rows_empty=True)
        return _SnapshotLoadResult(
            rows=[],
            partition_date=price_date,
            partition_computed_at=None,
            degradation_reason=reason,
            coverage_label=coverage_label,
        )

    from app.mcp_server.tooling.fundamentals._support_resistance import (
        get_support_resistance_impl,
    )

    semaphore = asyncio.Semaphore(_LIVE_CHECK_CONCURRENCY)

    async def _check(symbol: str) -> dict[str, Any] | None:
        async with semaphore:
            try:
                return await get_support_resistance_impl(symbol, market="kr")
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "support_proximity: get_support_resistance failed for %s: %s",
                    symbol,
                    exc,
                    exc_info=True,
                )
                return None

    sr_results = await asyncio.gather(
        *(_check(c["symbol"]) for c in bounded_candidates)
    )

    rows: list[dict[str, Any]] = []
    no_support_count = 0
    for candidate, sr in zip(bounded_candidates, sr_results, strict=True):
        if sr is None or sr.get("error"):
            continue
        supports = sr.get("supports") or []
        if not supports:
            # ROB-976: no clustered level below current price is a real, expected
            # outcome (e.g. a fresh 52-week high has nothing to anchor below it),
            # never a crash. Excluded from the ranked list, not fabricated.
            no_support_count += 1
            continue
        nearest = supports[0]
        distance_pct = abs(float(nearest.get("distance_pct") or 0.0))
        rows.append(
            {
                "symbol": candidate["symbol"],
                "market": "kr",
                "name": candidate["name"],
                "sector": candidate["sector"],
                "close": candidate["latest_close"],
                "latest_close": candidate["latest_close"],
                "prev_close": candidate["prev_close"],
                "change_rate": candidate["change_rate"],
                "change_amount": candidate["change_amount"],
                "volume": candidate["daily_volume"],
                "market_cap": candidate["market_cap"],
                "_market_cap_source": market_cap_source,
                "dist_to_support_pct": round(distance_pct, 2),
                "support_price": nearest.get("price"),
                "support_kind": ",".join(nearest.get("sources") or []) or None,
                "support_strength": nearest.get("strength"),
                "snapshot_date": price_date,
                "_screener_snapshot_state": row_state,
            }
        )

    rows.sort(key=lambda row: row["dist_to_support_pct"])
    rows = rows[:limit]

    reason, coverage_label = _partition_degradation(price_hp, rows_empty=not rows)
    if reason is None and not rows and no_support_count:
        # Every checked candidate had no support below price this round — an
        # honest "no qualifiers", not a partition problem.
        reason = "healthy_no_matches"

    return _SnapshotLoadResult(
        rows=rows,
        partition_date=price_date,
        partition_computed_at=None,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
