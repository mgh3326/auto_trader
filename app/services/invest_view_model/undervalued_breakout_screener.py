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
# ROB-440 PR3: US date-recency parity — a NEW 52-week high made within 20 *trading*
# sessions (XNYS), matching the KR/Toss "신고가 경신" definition (ROB-432) instead of
# the price-proximity proxy.
_NEW_HIGH_RECENCY_TRADING_DAYS = 20


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


def _new_high_age_trading_days(
    high_date: dt.date | None, partition_date: dt.date, market: str
) -> int | None:
    """Trading sessions from the 52-week-high date to the partition date (holiday-aware,
    XNYS/XKRX via session_calendar). Smaller = a more recent new 52-week high. None
    (fail-closed → excluded) when the date is missing, in the future, or out of the
    calendar's range / any calendar error."""
    if high_date is None or high_date > partition_date:
        return None
    from app.services.market_events.session_calendar import trading_sessions_in_range

    sessions = trading_sessions_in_range(market, high_date, partition_date)
    if not sessions:
        return None
    return sum(1 for s in sessions if s > high_date)


async def load_undervalued_breakout_from_snapshots(
    session: AsyncSession | None,
    *,
    market: str,
    limit: int = 20,
    today_market_date: dt.date | None = None,
) -> list[dict[str, Any]] | None:
    """Toss-parity 저평가 탈출 rows, or None when no valuation partition exists.

    ROB-440 Part 2: market-parameterized for US (proximity definition: close >=
    high_52w * 0.95). KR display uses the tvscreener date-recency loader; this
    market_valuation-backed loader serves US (+ KR reports/PIT). high_52w(price) +
    latest_close required; NULL → fail-closed excluded."""
    if session is None or market not in {"kr", "us"}:
        return None

    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
        served_partition_degraded,
    )

    val_hp = await resolve_healthy_partition(
        session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market=market,
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
            "undervalued_breakout: latest price date lookup failed: %s",
            exc,
            exc_info=True,
        )
        return None

    cand_stmt = (
        sa.select(
            MarketValuationSnapshot.symbol,
            MarketValuationSnapshot.per,
            MarketValuationSnapshot.pbr,
            MarketValuationSnapshot.high_52w,
            MarketValuationSnapshot.high_52w_date,
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
            MarketValuationSnapshot.market == market,
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
    if (
        market == "kr" and symbols
    ):  # ROB-440 Part 2: KR name/common-stock filter is KR-only
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

        today_market_date = today_trading_date(market, now=datetime.now(UTC))
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
        if market == "kr" and not _is_kr_toss_common_stock(sym, name):
            continue
        prox = _near_high_proximity(r["latest_close"], r["high_52w"])
        age: int | None = None
        if market == "us":
            # ROB-440 PR3: date-recency parity — a NEW 52w high made within 20 XNYS
            # trading sessions (Toss "신고가 경신"), not the price-proximity proxy.
            age = _new_high_age_trading_days(r["high_52w_date"], val_date, "us")
            if age is None or age > _NEW_HIGH_RECENCY_TRADING_DAYS:
                continue
        # KR reports/PIT: price-proximity proxy (close >= 95% of the 52-week high).
        elif not _passes_near_high(r["latest_close"], r["high_52w"], _NEAR_HIGH_RATIO):
            continue
        seen.add(sym)
        rows.append(
            {
                "symbol": sym,
                "market": market,
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
                "new_high_age_trading_days": age,  # ROB-440 PR3 (US date-recency)
                "market_cap": float(r["market_cap"])
                if r["market_cap"] is not None
                else None,
                "snapshot_date": val_date,
                "_screener_snapshot_state": state,
            }
        )

    if market == "us":
        # ROB-440 PR3: Toss 저평가 탈출 default order = cheapest PER asc; tiebreak by
        # the most recent new high (smaller age) then symbol.
        rows.sort(
            key=lambda x: (
                x["per"] if x["per"] is not None else float("inf"),
                x["new_high_age_trading_days"]
                if x["new_high_age_trading_days"] is not None
                else 10**9,
                x["symbol"],
            )
        )
    else:
        # KR reports/PIT: rank by proximity to the 52-week high (desc), then PER asc.
        rows.sort(
            key=lambda x: (
                x["high_52w_proximity"] is None,
                -(x["high_52w_proximity"] or 0.0),
                x["per"] if x["per"] is not None else float("inf"),
                x["symbol"],
            )
        )
    return rows[:limit]
