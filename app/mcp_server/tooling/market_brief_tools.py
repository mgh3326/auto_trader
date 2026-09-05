"""Market brief and reports MCP tool implementations."""

from __future__ import annotations

import logging
from typing import Any, cast

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.models.analysis import StockAnalysisResult, StockInfo

logger = logging.getLogger(__name__)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


async def get_latest_market_brief(
    symbols: list[str] | None = None,
    market: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    """Get a concise market summary for recent analysis results.

    Returns current decision, confidence, and key price levels for each symbol.
    symbols: optional list of symbols to filter (e.g. ['005930', 'AAPL']).
    market: optional filter by instrument_type ('equity_kr', 'equity_us', 'crypto').
    limit: max number of symbols to return (default 10).
    """
    try:
        async with _session_factory()() as db:
            from sqlalchemy import func

            latest_subq = (
                select(
                    StockAnalysisResult.stock_info_id,
                    func.max(StockAnalysisResult.created_at).label("max_created"),
                )
                .group_by(StockAnalysisResult.stock_info_id)
                .subquery()
            )

            stmt = (
                select(StockAnalysisResult, StockInfo)
                .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
                .join(
                    latest_subq,
                    (StockAnalysisResult.stock_info_id == latest_subq.c.stock_info_id)
                    & (StockAnalysisResult.created_at == latest_subq.c.max_created),
                )
                .order_by(desc(StockAnalysisResult.created_at))
            )

            if symbols:
                normalized = [s.strip().upper() for s in symbols]
                stmt = stmt.where(StockInfo.symbol.in_(normalized))

            if market:
                stmt = stmt.where(StockInfo.instrument_type == market)

            stmt = stmt.limit(limit)

            result = await db.execute(stmt)
            rows = result.all()

            briefs = []
            for analysis, info in rows:
                brief = {
                    "symbol": info.symbol,
                    "name": info.name,
                    "instrument_type": info.instrument_type,
                    "decision": analysis.decision,
                    "confidence": analysis.confidence,
                    "buy_range": _price_range(
                        analysis.appropriate_buy_min,
                        analysis.appropriate_buy_max,
                    ),
                    "sell_range": _price_range(
                        analysis.appropriate_sell_min,
                        analysis.appropriate_sell_max,
                    ),
                    "analyzed_at": (
                        analysis.created_at.isoformat() if analysis.created_at else None
                    ),
                }
                briefs.append(brief)

            summary = {
                "total": len(briefs),
                "buy_count": sum(1 for b in briefs if b["decision"] == "buy"),
                "hold_count": sum(1 for b in briefs if b["decision"] == "hold"),
                "sell_count": sum(1 for b in briefs if b["decision"] == "sell"),
                "avg_confidence": (
                    round(sum(b["confidence"] for b in briefs) / len(briefs), 1)
                    if briefs
                    else 0
                ),
            }

            return {
                "success": True,
                "briefs": briefs,
                "summary": summary,
            }

    except Exception as exc:
        logger.exception("get_latest_market_brief failed")
        return {"success": False, "error": f"get_latest_market_brief failed: {exc}"}


async def get_market_reports(
    symbol: str,
    days: int = 7,
    limit: int = 10,
) -> dict[str, Any]:
    """Get detailed analysis reports for a specific symbol.

    Returns full analysis history including reasons, price ranges, and detailed text.
    symbol: the symbol to query (e.g. '005930', 'AAPL', 'KRW-BTC').
    days: how many days of history to include (default 7).
    limit: max number of reports to return (default 10).
    """
    symbol = (symbol or "").strip().upper()
    if not symbol:
        return {"success": False, "error": "symbol is required"}

    try:
        async with _session_factory()() as db:
            from datetime import timedelta

            from app.core.timezone import now_kst

            cutoff = now_kst() - timedelta(days=days)

            stmt = (
                select(StockAnalysisResult, StockInfo)
                .join(StockInfo, StockAnalysisResult.stock_info_id == StockInfo.id)
                .where(
                    StockInfo.symbol == symbol,
                    StockAnalysisResult.created_at >= cutoff,
                )
                .order_by(desc(StockAnalysisResult.created_at))
                .limit(limit)
            )

            result = await db.execute(stmt)
            rows = result.all()

            if not rows:
                return {
                    "success": True,
                    "symbol": symbol,
                    "reports": [],
                    "message": f"No analysis reports found for {symbol} in the last {days} days",
                }

            reports = []
            for analysis, info in rows:
                report = {
                    "id": analysis.id,
                    "symbol": info.symbol,
                    "name": info.name,
                    "model_name": analysis.model_name,
                    "decision": analysis.decision,
                    "confidence": analysis.confidence,
                    "price_analysis": {
                        "appropriate_buy": _price_range(
                            analysis.appropriate_buy_min,
                            analysis.appropriate_buy_max,
                        ),
                        "appropriate_sell": _price_range(
                            analysis.appropriate_sell_min,
                            analysis.appropriate_sell_max,
                        ),
                        "buy_hope": _price_range(
                            analysis.buy_hope_min,
                            analysis.buy_hope_max,
                        ),
                        "sell_target": _price_range(
                            analysis.sell_target_min,
                            analysis.sell_target_max,
                        ),
                    },
                    "reasons": analysis.reasons,
                    "detailed_text": analysis.detailed_text,
                    "analyzed_at": (
                        analysis.created_at.isoformat() if analysis.created_at else None
                    ),
                }
                reports.append(report)

            decision_trend = [r["decision"] for r in reports]

            return {
                "success": True,
                "symbol": symbol,
                "name": rows[0][1].name,
                "reports": reports,
                "trend": {
                    "total_reports": len(reports),
                    "decision_sequence": decision_trend,
                    "latest_decision": decision_trend[0] if decision_trend else None,
                    "latest_confidence": (
                        reports[0]["confidence"] if reports else None
                    ),
                },
            }

    except Exception as exc:
        logger.exception("get_market_reports failed")
        return {"success": False, "error": f"get_market_reports failed: {exc}"}


def _price_range(
    min_val: float | None, max_val: float | None
) -> dict[str, float | None]:
    return {"min": min_val, "max": max_val}
