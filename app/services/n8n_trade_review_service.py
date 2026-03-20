"""Trade review service — UPSERT trades, INSERT snapshots/reviews, compute stats."""

from __future__ import annotations

import logging
import re
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.models.review import Trade, TradeReview, TradeSnapshot

logger = logging.getLogger(__name__)

_INSTRUMENT_MAP = {
    "crypto": "crypto",
    "equity_kr": "equity_kr",
    "equity_us": "equity_us",
    "kr": "equity_kr",
    "us": "equity_us",
}

# Instrument type reverse mapping for response
_REVERSE_INSTRUMENT_MAP = {
    "crypto": "crypto",
    "equity_kr": "kr",
    "equity_us": "us",
}


def parse_period(period: str) -> timedelta:
    """Parse duration string like '7d', '30d' into timedelta. Defaults to 7d."""
    match = re.match(r"^(\d+)d$", period.strip())
    if match:
        return timedelta(days=int(match.group(1)))
    return timedelta(days=7)


async def save_trade_reviews(
    session: AsyncSession,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    saved = 0
    skipped = 0
    errors: list[dict[str, Any]] = []

    for item in items:
        order_id = item.get("order_id")
        if not order_id:
            errors.append(
                {
                    "order_id": order_id,
                    "error": "order_id is required (null not allowed)",
                }
            )
            continue

        try:
            instrument = _INSTRUMENT_MAP.get(
                item.get("instrument_type", ""), item.get("instrument_type", "")
            )

            filled_at_str = item.get("filled_at", "")
            try:
                from datetime import datetime

                trade_date = datetime.fromisoformat(
                    filled_at_str.replace("Z", "+00:00")
                )
                if trade_date.tzinfo is None:
                    trade_date = trade_date.replace(tzinfo=KST)
            except (ValueError, AttributeError):
                trade_date = now_kst()

            stmt = (
                pg_insert(Trade)
                .values(
                    trade_date=trade_date,
                    symbol=item.get("symbol", ""),
                    instrument_type=instrument,
                    side=item.get("side", "buy"),
                    price=item.get("price", 0),
                    quantity=item.get("quantity", 0),
                    total_amount=item.get("total_amount", 0),
                    fee=item.get("fee", 0),
                    currency=item.get("currency", "KRW"),
                    account=item.get("account", ""),
                    order_id=order_id,
                )
                .on_conflict_do_nothing(
                    constraint="uq_review_trades_account_order",
                )
            )

            result = await session.execute(stmt)
            await session.flush()

            trade_id: int | None = None
            if result.inserted_primary_key and result.inserted_primary_key[0]:
                trade_id = result.inserted_primary_key[0]
            else:
                existing = await session.scalars(
                    select(Trade.id).where(
                        Trade.account == item.get("account", ""),
                        Trade.order_id == order_id,
                    )
                )
                trade_id = existing.first()
                if not trade_id:
                    skipped += 1
                    continue

            indicators = item.get("indicators")
            if indicators and isinstance(indicators, dict):
                existing_snap = await session.scalars(
                    select(TradeSnapshot.id).where(TradeSnapshot.trade_id == trade_id)
                )
                if not existing_snap.first():
                    snapshot = TradeSnapshot(
                        trade_id=trade_id,
                        rsi_14=indicators.get("rsi_14"),
                        rsi_7=indicators.get("rsi_7"),
                        ema_20=indicators.get("ema_20"),
                        ema_200=indicators.get("ema_200"),
                        macd=indicators.get("macd"),
                        macd_signal=indicators.get("macd_signal"),
                        adx=indicators.get("adx"),
                        stoch_rsi_k=indicators.get("stoch_rsi_k"),
                        volume_ratio=indicators.get("volume_ratio"),
                        fear_greed=indicators.get("fear_greed"),
                    )
                    session.add(snapshot)

            review_type = item.get("review_type", "daily")
            existing_review = await session.scalars(
                select(TradeReview.id).where(
                    TradeReview.trade_id == trade_id,
                    TradeReview.review_type == review_type,
                )
            )
            if not existing_review.first():
                review = TradeReview(
                    trade_id=trade_id,
                    review_date=now_kst(),
                    price_at_review=item.get("price_at_review"),
                    pnl_pct=item.get("pnl_pct"),
                    verdict=item.get("verdict", "neutral"),
                    comment=item.get("comment"),
                    review_type=review_type,
                )
                session.add(review)
                saved += 1
            else:
                skipped += 1

        except Exception as exc:
            logger.warning("Failed to save review for order %s: %s", order_id, exc)
            errors.append({"order_id": order_id, "error": str(exc)})
            continue

    await session.commit()
    return {"saved_count": saved, "skipped_count": skipped, "errors": errors}


async def get_trade_review_stats(
    session: AsyncSession,
    period: str = "week",
    market: str | None = None,
) -> dict[str, Any]:
    now = now_kst()
    if period == "week":
        start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif period == "quarter":
        quarter_month = ((now.month - 1) // 3) * 3 + 1
        start = now.replace(
            month=quarter_month, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    else:
        start = (now - timedelta(days=7)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    period_label = f"{start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"

    base_filter = [Trade.trade_date >= start, Trade.trade_date <= now]
    if market:
        itype = _INSTRUMENT_MAP.get(market, market)
        base_filter.append(Trade.instrument_type == itype)

    stmt = (
        select(Trade, TradeReview)
        .join(TradeReview, Trade.id == TradeReview.trade_id)
        .where(*base_filter)
        .order_by(TradeReview.pnl_pct.desc().nulls_last())
    )
    result = await session.execute(stmt)
    rows = result.all()

    empty_stats = {
        "period": period_label,
        "total_trades": 0,
        "buy_count": 0,
        "sell_count": 0,
        "win_rate": None,
        "avg_pnl_pct": None,
        "best_trade": None,
        "worst_trade": None,
        "by_verdict": {},
        "by_rsi_zone": {},
    }

    if not rows:
        return empty_stats

    trades_data = []
    for trade, review in rows:
        trades_data.append(
            {
                "symbol": trade.symbol,
                "side": trade.side,
                "pnl_pct": float(review.pnl_pct)
                if review.pnl_pct is not None
                else None,
                "verdict": review.verdict,
            }
        )

    total = len(trades_data)
    buy_count = sum(1 for t in trades_data if t["side"] == "buy")
    sell_count = total - buy_count

    pnl_values = [t["pnl_pct"] for t in trades_data if t["pnl_pct"] is not None]
    wins = sum(1 for p in pnl_values if p > 0)
    win_rate = round((wins / len(pnl_values)) * 100, 1) if pnl_values else None
    avg_pnl = round(sum(pnl_values) / len(pnl_values), 2) if pnl_values else None

    best = max(trades_data, key=lambda t: t.get("pnl_pct") or float("-inf"))
    worst = min(trades_data, key=lambda t: t.get("pnl_pct") or float("inf"))

    by_verdict: dict[str, int] = {}
    for t in trades_data:
        v = t.get("verdict", "neutral")
        by_verdict[v] = by_verdict.get(v, 0) + 1

    rsi_stmt = (
        select(TradeSnapshot.rsi_14, TradeReview.pnl_pct)
        .join(TradeReview, TradeSnapshot.trade_id == TradeReview.trade_id)
        .join(Trade, Trade.id == TradeSnapshot.trade_id)
        .where(*base_filter)
        .where(TradeSnapshot.rsi_14.is_not(None))
    )
    rsi_result = await session.execute(rsi_stmt)
    rsi_rows = rsi_result.all()

    zones: dict[str, list[float]] = {
        "oversold_lt30": [],
        "neutral_30_50": [],
        "overbought_gt50": [],
    }
    for rsi_val, pnl_val in rsi_rows:
        rsi_f = float(rsi_val)
        pnl_f = float(pnl_val) if pnl_val is not None else 0
        if rsi_f < 30:
            zones["oversold_lt30"].append(pnl_f)
        elif rsi_f <= 50:
            zones["neutral_30_50"].append(pnl_f)
        else:
            zones["overbought_gt50"].append(pnl_f)

    by_rsi_zone = {}
    for zone_name, pnl_list in zones.items():
        if pnl_list:
            zone_wins = sum(1 for p in pnl_list if p > 0)
            by_rsi_zone[zone_name] = {
                "count": len(pnl_list),
                "avg_pnl": round(sum(pnl_list) / len(pnl_list), 2),
                "win_rate": round((zone_wins / len(pnl_list)) * 100, 1),
            }

    return {
        "period": period_label,
        "total_trades": total,
        "buy_count": buy_count,
        "sell_count": sell_count,
        "win_rate": win_rate,
        "avg_pnl_pct": avg_pnl,
        "best_trade": {"symbol": best["symbol"], "pnl_pct": best.get("pnl_pct")},
        "worst_trade": {"symbol": worst["symbol"], "pnl_pct": worst.get("pnl_pct")},
        "by_verdict": by_verdict,
        "by_rsi_zone": by_rsi_zone,
    }


async def get_trade_reviews(
    session: AsyncSession,
    period: str = "7d",
    market: str | None = None,
    symbol: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Query saved trade reviews with optional filters."""
    delta = parse_period(period)
    now = now_kst()
    start = now - delta

    period_label = f"{start.strftime('%Y-%m-%d')} ~ {now.strftime('%Y-%m-%d')}"

    # Build filters
    filters = [Trade.trade_date >= start, Trade.trade_date <= now]

    if market:
        itype = _INSTRUMENT_MAP.get(market, market)
        filters.append(Trade.instrument_type == itype)

    if symbol:
        filters.append(Trade.symbol == symbol.upper())

    # Query: Trade JOIN TradeReview LEFT JOIN TradeSnapshot
    stmt = (
        select(Trade, TradeReview, TradeSnapshot)
        .join(TradeReview, Trade.id == TradeReview.trade_id)
        .outerjoin(TradeSnapshot, Trade.id == TradeSnapshot.trade_id)
        .where(*filters)
        .order_by(Trade.trade_date.desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    rows = result.all()

    reviews = []
    for trade, review, snapshot in rows:
        market_code = _REVERSE_INSTRUMENT_MAP.get(
            trade.instrument_type.value
            if hasattr(trade.instrument_type, "value")
            else str(trade.instrument_type),
            "crypto",
        )

        indicators = None
        if snapshot:
            indicators = {
                "rsi_14": float(snapshot.rsi_14)
                if snapshot.rsi_14 is not None
                else None,
                "rsi_7": float(snapshot.rsi_7) if snapshot.rsi_7 is not None else None,
                "ema_20": float(snapshot.ema_20)
                if snapshot.ema_20 is not None
                else None,
                "ema_200": float(snapshot.ema_200)
                if snapshot.ema_200 is not None
                else None,
                "macd": float(snapshot.macd) if snapshot.macd is not None else None,
                "macd_signal": float(snapshot.macd_signal)
                if snapshot.macd_signal is not None
                else None,
                "adx": float(snapshot.adx) if snapshot.adx is not None else None,
                "stoch_rsi_k": float(snapshot.stoch_rsi_k)
                if snapshot.stoch_rsi_k is not None
                else None,
                "volume_ratio": float(snapshot.volume_ratio)
                if snapshot.volume_ratio is not None
                else None,
                "fear_greed": int(snapshot.fear_greed)
                if snapshot.fear_greed is not None
                else None,
            }

        reviews.append(
            {
                "order_id": trade.order_id or "",
                "symbol": trade.symbol,
                "market": market_code,
                "side": trade.side,
                "price": float(trade.price),
                "quantity": float(trade.quantity),
                "total_amount": float(trade.total_amount),
                "fee": float(trade.fee),
                "currency": trade.currency,
                "filled_at": trade.trade_date.isoformat(),
                "verdict": review.verdict,
                "pnl_pct": float(review.pnl_pct)
                if review.pnl_pct is not None
                else None,
                "comment": review.comment,
                "review_type": review.review_type,
                "review_date": review.review_date.isoformat(),
                "indicators": indicators,
            }
        )

    return {
        "period": period_label,
        "total_count": len(reviews),
        "reviews": reviews,
        "errors": [],
    }
