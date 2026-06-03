# app/services/invest_view_model/undervalued_breakout_screener.py
"""Read-only loader for the 저평가 탈출 (Toss undervalued-breakout parity) preset.

Toss rule: market = kr, 0 < per <= 10, 0 < pbr <= 1, and the price is near the
52-week high (close >= high_52w * 0.95). per/pbr/high_52w come from
market_valuation_snapshots (naver_finance); latest_close from invest_screener_snapshots.
Valuation-only — NO fundamentals dependency. NULL per/pbr/close/high_52w are excluded
(fail-closed; never fabricate a qualifier).
"""

from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.market_valuation_snapshot import MarketValuationSnapshot

logger = logging.getLogger(__name__)

_MAX_PER = Decimal("10")
_MAX_PBR = Decimal("1")
_NEAR_HIGH_RATIO = Decimal("0.95")  # close within 5% of (or above) the 52-week high


def _near_high_proximity(
    latest_close: Decimal | None, high_52w: Decimal | None
) -> Decimal | None:
    """close / high_52w; None when either is missing or high_52w <= 0."""
    if latest_close is None or high_52w is None or high_52w <= 0:
        return None
    return latest_close / high_52w


def _passes_near_high(
    latest_close: Decimal | None, high_52w: Decimal | None, ratio: Decimal
) -> bool:
    prox = _near_high_proximity(latest_close, high_52w)
    return prox is not None and prox >= ratio


async def load_undervalued_breakout_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 20,
    today_market_date: dt.date | None = None,
) -> list[dict[str, Any]] | None:
    """Toss-parity 저평가 탈출 rows, or None when no KR valuation partition exists."""
    if session is None or market != "kr":
        return None

    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
    )

    val_hp = await resolve_healthy_partition(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="kr",
    )
    val_date = val_hp.partition_date if val_hp else None
    if val_date is None:
        return None
    partition_degraded = bool(val_hp and (val_hp.is_fallback or not val_hp.healthy))

    latest_price_stmt = sa.select(
        sa.func.max(InvestScreenerSnapshot.snapshot_date)
    ).where(InvestScreenerSnapshot.market == "kr")
    try:
        price_date = (await session.execute(latest_price_stmt)).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "undervalued_breakout: latest price date lookup failed: %s", exc, exc_info=True
        )
        return None

    cand_stmt = (
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.per,
            MarketValuationSnapshot.pbr,
            MarketValuationSnapshot.high_52w,
            MarketValuationSnapshot.market_cap,
            MarketValuationSnapshot.source,
            InvestScreenerSnapshot.latest_close,
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
            MarketValuationSnapshot.market == "kr",
            MarketValuationSnapshot.snapshot_date == val_date,
            MarketValuationSnapshot.per > 0,
            MarketValuationSnapshot.per <= _MAX_PER,
            MarketValuationSnapshot.pbr > 0,
            MarketValuationSnapshot.pbr <= _MAX_PBR,
        )
        .order_by(
            MarketValuationSnapshot.per.asc().nullslast(),
            MarketValuationSnapshot.symbol.asc(),
            MarketValuationSnapshot.source.asc(),
        )
        .limit(max(limit * 6, limit + 60))
    )
    try:
        cand_rows = list((await session.execute(cand_stmt)).mappings().all())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "undervalued_breakout: candidate query failed: %s", exc, exc_info=True
        )
        return None

    symbols = [r["symbol"] for r in cand_rows]
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
            logger.warning(
                "undervalued_breakout: name lookup failed: %s", exc, exc_info=True
            )

    from app.services.invest_view_model.screener_service import _is_kr_toss_common_stock

    if today_market_date is None:
        from datetime import UTC, datetime

        from app.services.invest_screener_snapshots.freshness import today_trading_date

        today_market_date = today_trading_date("kr", now=datetime.now(UTC))
    state = "fresh" if val_date == today_market_date else "stale"
    if partition_degraded:
        from app.services.invest_screener_snapshots.partition_health import (
            cap_degraded,
        )

        state = cap_degraded(state)

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in cand_rows:
        sym = r["symbol"]
        if sym in seen:
            continue
        name = name_map.get(sym)
        if not _is_kr_toss_common_stock(sym, name):
            continue
        # 신고가 근접 (fail-closed on NULL close / high_52w)
        if not _passes_near_high(r["latest_close"], r["high_52w"], _NEAR_HIGH_RATIO):
            continue
        seen.add(sym)
        prox = _near_high_proximity(r["latest_close"], r["high_52w"])
        rows.append(
            {
                "symbol": sym,
                "market": "kr",
                "name": name,
                "latest_close": float(r["latest_close"])
                if r["latest_close"] is not None
                else None,
                "change_rate": float(r["change_rate"])
                if r["change_rate"] is not None
                else None,
                "volume": r["daily_volume"],
                "per": float(r["per"]) if r["per"] is not None else None,
                "pbr": float(r["pbr"]) if r["pbr"] is not None else None,
                "high_52w": float(r["high_52w"]) if r["high_52w"] is not None else None,
                "high_52w_proximity": float(prox) if prox is not None else None,
                "market_cap": float(r["market_cap"])
                if r["market_cap"] is not None
                else None,
                "snapshot_date": val_date,
                "_screener_snapshot_state": state,
            }
        )

    # Rank by proximity to the 52-week high (desc), then cheapest PER (asc).
    rows.sort(
        key=lambda x: (
            x["high_52w_proximity"] is None,
            -(x["high_52w_proximity"] or 0.0),
            x["per"] if x["per"] is not None else float("inf"),
            x["symbol"],
        )
    )
    return rows[:limit]
