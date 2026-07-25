"""Read-only loader for the 고수익 저평가 (Toss 고수익 저평가 parity) preset.

Filters the latest ``market_valuation_snapshots`` partition by Toss's
high-yield-value rule:

    market = kr | us   (ROB-427 PR3: US backed by Yahoo valuation snapshots)
    roe >= 15          (percent; KR=Naver ROE(%), US=Yahoo ROE)
    0 < per <= 10
    sort by roe desc, per asc, symbol asc

ROE/PER come from ``market_valuation_snapshots`` (KR=``naver_finance``, US=``yahoo``).
The latest ``invest_screener_snapshots`` price row is LEFT-joined for display
only — a missing price row never drops a qualifying valuation row. NULL roe/per
are excluded by the SQL predicate (fail-closed; never fabricate a qualifier).
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

_MIN_ROE = 15.0  # percent
_MAX_PER = 10.0


async def load_high_yield_value_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 20,
    today_market_date: dt.date | None = None,
) -> list[dict[str, Any]] | None:
    """Return Toss-parity 고수익 저평가 rows or None when no valuation partition exists.

    None  -> caller reports dataState=missing (no valuation snapshot at all).
    []    -> latest partition exists but no qualifiers (caller renders empty + stale).
    Rows  -> ordered by roe desc, per asc, symbol asc.
    """
    # ROB-427 PR3: KR + US. Both back this preset with market_valuation_snapshots
    # (KR=naver_finance, US=yahoo) — ROE/PER are vendor-agnostic, so the same query
    # serves both once the hardcoded "kr" literals below are parameterized.
    if session is None or market not in {"kr", "us"}:
        return None

    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
        served_partition_degraded,
    )
    from app.services.market_valuation_snapshots.repository import metric_rich_filter

    val_hp = await resolve_healthy_partition(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market=market,
        row_filter=metric_rich_filter(),  # ROB-551: skip toss-only partitions
    )
    val_date = val_hp.partition_date if val_hp else None
    if val_date is None:
        return None
    # ROB-440: a healthy fallback partition is NOT degraded (date check handles
    # staleness); only an unhealthy served partition is.
    partition_degraded = served_partition_degraded(val_hp)

    latest_price_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == market)
    try:
        price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "high_yield_value: latest price date lookup failed: %s", exc, exc_info=True
        )
        return None

    candidate_stmt = (
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.per,
            MarketValuationSnapshot.roe,
            MarketValuationSnapshot.pbr,
            MarketValuationSnapshot.market_cap,
            MarketValuationSnapshot.source,
            InvestScreenerSnapshot.latest_close,
            InvestScreenerSnapshot.prev_close,
            InvestScreenerSnapshot.change_rate,
            InvestScreenerSnapshot.daily_volume,
        )
        .outerjoin(
            InvestScreenerSnapshot,
            sa.and_(
                InvestScreenerSnapshot.market == MarketValuationSnapshot.market,
                InvestScreenerSnapshot.symbol == MarketValuationSnapshot.symbol,
                InvestScreenerSnapshot.snapshot_date == price_date,
            ),
        )
        .where(
            MarketValuationSnapshot.market == market,
            MarketValuationSnapshot.snapshot_date == val_date,
            MarketValuationSnapshot.roe >= _MIN_ROE,
            MarketValuationSnapshot.per > 0,
            MarketValuationSnapshot.per <= _MAX_PER,
        )
    )
    if market == "us":
        # ROB-440: drop yahoo micro-cap / ROE-artifact outliers (DCX 1177% @ $48M).
        from app.services.invest_view_model.us_quality_guards import (
            apply_us_valuation_quality_guards,
        )

        candidate_stmt = apply_us_valuation_quality_guards(
            candidate_stmt, uses_roe=True
        )
    candidate_stmt = candidate_stmt.order_by(
        MarketValuationSnapshot.roe.desc().nullslast(),
        MarketValuationSnapshot.per.asc().nullslast(),
        MarketValuationSnapshot.symbol.asc(),
        MarketValuationSnapshot.source.asc(),
    ).limit(max(limit * 4, limit + 40))
    try:
        result = await session.execute(candidate_stmt)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "high_yield_value: candidate query failed: %s", exc, exc_info=True
        )
        return None
    candidate_rows = list(result.mappings().all())

    symbols = [r["symbol"] for r in candidate_rows]
    name_map: dict[str, str] = {}
    sector_map: dict[str, str] = {}
    # KR name hydration + the KR common-stock guard are KR-specific (KRX universe +
    # Korean-name ETF/preferred heuristic). US mirrors the consecutive_gainers loader:
    # no KR-universe lookup; the shared row builder fills the US name downstream.
    if market == "kr" and symbols:
        from app.models.symbol_sectors import SymbolSector

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
            logger.warning(
                "high_yield_value: name lookup failed: %s", exc, exc_info=True
            )

    # Imported inside the function to avoid a circular import at module load.
    from app.services.invest_view_model.screener_service import (
        _is_kr_toss_common_stock,
    )

    # Valuation-snapshot freshness: fresh only when the latest valuation
    # partition is the current trading date; older partitions surface as stale,
    # never as fresh (honest staleness, never fabricated freshness).
    if today_market_date is None:
        from datetime import UTC, datetime

        from app.services.invest_screener_snapshots.freshness import (
            today_trading_date,
        )

        today_market_date = today_trading_date(market, now=datetime.now(UTC))
    state = "fresh" if val_date == today_market_date else "stale"
    if partition_degraded:
        from app.services.invest_screener_snapshots.partition_health import (
            cap_degraded,
        )

        state = cap_degraded(state)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in candidate_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        name = name_map.get(sym)
        if market == "kr" and not _is_kr_toss_common_stock(sym, name):
            continue
        seen.add(sym)
        rows.append(
            {
                "symbol": sym,
                "market": market,
                "name": name,
                "sector": sector_map.get(sym),
                "latest_close": (
                    float(r["latest_close"]) if r["latest_close"] is not None else None
                ),
                "close": (
                    float(r["latest_close"]) if r["latest_close"] is not None else None
                ),
                "prev_close": (
                    float(r["prev_close"]) if r["prev_close"] is not None else None
                ),
                "change_rate": (
                    float(r["change_rate"]) if r["change_rate"] is not None else None
                ),
                "volume": r["daily_volume"],
                "per": float(r["per"]) if r["per"] is not None else None,
                "roe": float(r["roe"]) if r["roe"] is not None else None,
                "pbr": float(r["pbr"]) if r["pbr"] is not None else None,
                "market_cap": (
                    float(r["market_cap"]) if r["market_cap"] is not None else None
                ),
                "snapshot_date": val_date,
                "_screener_snapshot_state": state,
            }
        )
        if len(rows) >= limit:
            break
    return rows
