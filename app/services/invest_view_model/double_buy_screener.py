"""Read-only loader for the 쌍끌이 매수 (Toss screenId=18 parity) preset.

Joins the latest investor_flow_snapshots row with the latest
invest_screener_snapshots row per symbol and applies the Toss-parity filter
(Interpretation A, locked 2026-05-20 under Task 0 safer-fallback rule — see
plan Decision 1):

    market = kr
    foreign_net  > 0   AND institution_net  > 0
    change_rate >= 0
    sort by change_rate desc, symbol asc
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.services.invest_view_model.screener_service import _SnapshotLoadResult

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.investor_flow_snapshot import InvestorFlowSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)


async def load_double_buy_from_snapshots(
    session: AsyncSession | None, *, market: str, limit: int = 50
) -> _SnapshotLoadResult | None:
    """Return Toss-parity 쌍끌이 매수 rows or None when no snapshot partition exists.

    None  -> caller should report dataState=missing and warn that snapshots are absent.
    []    -> latest partition exists but no qualifiers (caller renders empty + stale).
    Rows  -> ordered by change_rate desc, symbol asc.
    """
    if session is None or market != "kr":
        return None

    from app.services.invest_screener_snapshots.partition_health import (
        active_universe_count,
        resolve_healthy_partition,
    )

    universe_count = await active_universe_count(session, market="kr")
    flow_hp = await resolve_healthy_partition(
        session,
        model=InvestorFlowSnapshot,
        date_col=InvestorFlowSnapshot.snapshot_date,
        market_col=InvestorFlowSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    price_hp = await resolve_healthy_partition(
        session,
        model=InvestScreenerSnapshot,
        date_col=InvestScreenerSnapshot.snapshot_date,
        market_col=InvestScreenerSnapshot.market,
        market="kr",
        universe_count=universe_count,
    )
    flow_date = flow_hp.partition_date if flow_hp else None
    price_date = price_hp.partition_date if price_hp else None
    if flow_date is None or price_date is None:
        return None
    partition_degraded = bool(
        flow_hp and (flow_hp.is_fallback or not flow_hp.healthy)
    ) or bool(price_hp and (price_hp.is_fallback or not price_hp.healthy))

    candidate_stmt = (
        sa.select(
            InvestorFlowSnapshot.symbol,
            InvestorFlowSnapshot.foreign_net,
            InvestorFlowSnapshot.institution_net,
            InvestorFlowSnapshot.individual_net,
            InvestorFlowSnapshot.double_buy,
            InvestorFlowSnapshot.foreign_consecutive_buy_days,
            InvestorFlowSnapshot.institution_consecutive_buy_days,
            InvestScreenerSnapshot.latest_close,
            InvestScreenerSnapshot.prev_close,
            InvestScreenerSnapshot.change_rate,
            InvestScreenerSnapshot.daily_volume,
            InvestScreenerSnapshot.snapshot_date.label("price_snapshot_date"),
            InvestorFlowSnapshot.snapshot_date.label("flow_snapshot_date"),
        )
        .join(
            InvestScreenerSnapshot,
            sa.and_(
                InvestScreenerSnapshot.market == InvestorFlowSnapshot.market,
                InvestScreenerSnapshot.symbol == InvestorFlowSnapshot.symbol,
                InvestScreenerSnapshot.snapshot_date == price_date,
            ),
        )
        .where(
            InvestorFlowSnapshot.market == "kr",
            InvestorFlowSnapshot.snapshot_date == flow_date,
            InvestorFlowSnapshot.foreign_net > 0,
            InvestorFlowSnapshot.institution_net > 0,
            sa.func.coalesce(InvestScreenerSnapshot.change_rate, 0) >= 0,
        )
        .order_by(
            InvestScreenerSnapshot.change_rate.desc().nullslast(),
            InvestorFlowSnapshot.symbol.asc(),
            InvestorFlowSnapshot.source.asc(),
        )
        .limit(max(limit * 4, limit + 40))
    )
    try:
        result = await session.execute(candidate_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning("double_buy: candidate query failed: %s", exc, exc_info=True)
        return None
    candidate_rows = list(result.mappings().all())

    symbols = [r["symbol"] for r in candidate_rows]
    name_map: dict[str, str] = {}
    if symbols:
        try:
            names = await session.execute(
                sa.select(KRSymbolUniverse.symbol, KRSymbolUniverse.name).where(
                    KRSymbolUniverse.symbol.in_(symbols),
                    KRSymbolUniverse.is_active.is_(True),
                )
            )
            name_map = {row.symbol: row.name for row in names.all()}
        except Exception as exc:  # noqa: BLE001
            logger.warning("double_buy: name lookup failed: %s", exc, exc_info=True)

    market_cap_map: dict[str, float] = {}
    market_cap_source: str | None = None
    if symbols:
        from app.services.invest_screener_snapshots.partition_health import (
            resolve_healthy_partition as _resolve_val_hp,
        )

        val_hp = await _resolve_val_hp(
            session,
            model=MarketValuationSnapshot,
            date_col=MarketValuationSnapshot.snapshot_date,
            market_col=MarketValuationSnapshot.market,
            market="kr",
            universe_count=universe_count,
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
                    "double_buy: market_cap lookup failed: %s", exc, exc_info=True
                )

    # Imported inside the function to avoid a circular import at module load
    # (screener_service imports from this module's neighborhood).
    from app.services.invest_view_model.screener_service import (
        _is_kr_toss_common_stock,
        _partition_degradation,
        _SnapshotLoadResult,
    )

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Multiple investor_flow sources may exist per (symbol, snapshot_date) due to
    # unique constraint scope. SQL ORDER BY source.asc() above ensures the first
    # row seen here is the alphabetically-earlier source — currently only
    # `naver_finance` is live, but this keeps the choice deterministic if `kis` or
    # other sources are added later.
    for r in candidate_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        name = name_map.get(sym)
        if not _is_kr_toss_common_stock(sym, name):
            continue
        seen.add(sym)
        state = (
            "fresh" if r["price_snapshot_date"] == r["flow_snapshot_date"] else "stale"
        )
        if partition_degraded:
            from app.services.invest_screener_snapshots.partition_health import (
                cap_degraded,
            )

            state = cap_degraded(state)
        rows.append(
            {
                "symbol": sym,
                "market": "kr",
                "name": name,
                "latest_close": (
                    float(r["latest_close"]) if r["latest_close"] is not None else None
                ),
                "prev_close": (
                    float(r["prev_close"]) if r["prev_close"] is not None else None
                ),
                "change_rate": (
                    float(r["change_rate"]) if r["change_rate"] is not None else None
                ),
                "volume": r["daily_volume"],
                "foreign_net": r["foreign_net"],
                "institution_net": r["institution_net"],
                "individual_net": r["individual_net"],
                "double_buy": r["double_buy"],
                "foreign_consecutive_buy_days": r["foreign_consecutive_buy_days"],
                "institution_consecutive_buy_days": r[
                    "institution_consecutive_buy_days"
                ],
                "snapshot_date": r["price_snapshot_date"],
                "flow_snapshot_date": r["flow_snapshot_date"],
                "_screener_snapshot_state": state,
                "market_cap": market_cap_map.get(sym),
                "_market_cap_source": (
                    market_cap_source if sym in market_cap_map else None
                ),
            }
        )
        if len(rows) >= limit:
            break

    # ROB-426 PR3: reason from the worst of the two partitions. Priority order
    # snapshot_missing > coverage_below_floor > older_fallback > healthy_no_matches.
    _priority = {
        "snapshot_missing": 0,
        "coverage_below_floor": 1,
        "older_fallback": 2,
        "healthy_no_matches": 3,
        None: 4,
    }
    flow_reason, flow_cov = _partition_degradation(flow_hp, rows_empty=not rows)
    price_reason, price_cov = _partition_degradation(price_hp, rows_empty=not rows)
    if _priority[flow_reason] <= _priority[price_reason]:
        reason, coverage_label = flow_reason, flow_cov
    else:
        reason, coverage_label = price_reason, price_cov

    return _SnapshotLoadResult(
        rows=rows,
        partition_date=price_date,
        partition_computed_at=None,
        degradation_reason=reason,
        coverage_label=coverage_label,
    )
