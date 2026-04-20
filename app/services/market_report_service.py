"""Market report persistence service — UPSERT reports to market_reports table."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.models.market_report import MarketReport

logger = logging.getLogger(__name__)


async def upsert_market_report(
    *,
    report_type: str,
    report_date: date,
    market: str,
    content: dict[str, Any],
    title: str | None = None,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    user_id: int | None = 1,
) -> int | None:
    now = now_kst().replace(tzinfo=None)

    async with AsyncSessionLocal() as session:
        stmt = (
            pg_insert(MarketReport.__table__)
            .values(
                report_type=report_type,
                report_date=report_date,
                market=market,
                title=title,
                content=content,
                summary=summary,
                metadata=metadata,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                constraint="uq_market_reports_type_date_market_user",
                set_={
                    "content": content,
                    "title": title,
                    "summary": summary,
                    "metadata": metadata,
                    "updated_at": now,
                },
            )
        )
        result = await session.execute(stmt)
        await session.commit()

        if result.inserted_primary_key:
            return result.inserted_primary_key[0]
        return None


async def save_daily_brief_report(result: dict[str, Any]) -> None:
    try:
        as_of_str = result.get("as_of", "")
        try:
            report_date = datetime.fromisoformat(as_of_str).date()
        except (ValueError, AttributeError):
            report_date = now_kst().date()

        await upsert_market_report(
            report_type="daily_brief",
            report_date=report_date,
            market="all",
            content=_serialize_result(result),
            title=f"Daily Brief — {result.get('date_fmt', '')}",
            summary=result.get("brief_text"),
        )
        logger.info("Saved daily_brief report for %s", report_date)
    except Exception:
        logger.exception("Failed to save daily_brief report to DB")


async def save_kr_morning_report(result: dict[str, Any]) -> None:
    try:
        as_of_str = result.get("as_of", "")
        try:
            report_date = datetime.fromisoformat(as_of_str).date()
        except (ValueError, AttributeError):
            report_date = now_kst().date()

        await upsert_market_report(
            report_type="kr_morning",
            report_date=report_date,
            market="kr",
            content=_serialize_result(result),
            title=f"KR Morning Report — {result.get('date_fmt', '')}",
            summary=result.get("brief_text"),
        )
        logger.info("Saved kr_morning report for %s", report_date)
    except Exception:
        logger.exception("Failed to save kr_morning report to DB")


async def save_crypto_scan_report(result: dict[str, Any]) -> None:
    try:
        report_date = now_kst().date()

        await upsert_market_report(
            report_type="crypto_scan",
            report_date=report_date,
            market="crypto",
            content=_serialize_result(result),
            title=f"Crypto Scan — {report_date.isoformat()}",
            summary=None,
        )
        logger.info("Saved crypto_scan report for %s", report_date)
    except Exception:
        logger.exception("Failed to save crypto_scan report to DB")


async def get_market_reports(
    *,
    report_type: str | None = None,
    market: str | None = None,
    days: int = 7,
    limit: int = 10,
) -> list[dict[str, Any]]:
    cutoff = now_kst().date() - timedelta(days=days)

    async with AsyncSessionLocal() as session:
        q = select(MarketReport).where(MarketReport.report_date >= cutoff)

        if report_type:
            q = q.where(MarketReport.report_type == report_type)
        if market:
            q = q.where(MarketReport.market == market)

        q = q.order_by(MarketReport.report_date.desc(), MarketReport.created_at.desc())
        q = q.limit(limit)

        result = await session.execute(q)
        reports = list(result.scalars().all())

    return [_report_to_dict(r) for r in reports]


async def get_latest_market_brief(
    *,
    market: str = "all",
) -> dict[str, Any] | None:
    async with AsyncSessionLocal() as session:
        q = select(MarketReport).where(MarketReport.report_type == "daily_brief")
        if market != "all":
            q = q.where(MarketReport.market == market)

        q = q.order_by(MarketReport.report_date.desc()).limit(1)

        result = await session.execute(q)
        report = result.scalars().first()

    if not report:
        return None
    return _report_to_dict(report)


def _report_to_dict(report: MarketReport) -> dict[str, Any]:
    return {
        "id": report.id,
        "report_type": report.report_type,
        "report_date": report.report_date.isoformat(),
        "market": report.market,
        "title": report.title,
        "content": report.content,
        "summary": report.summary,
        "metadata": report.metadata_,
        "created_at": report.created_at.isoformat() if report.created_at else None,
        "updated_at": report.updated_at.isoformat() if report.updated_at else None,
    }


def _serialize_result(result: dict[str, Any]) -> dict[str, Any]:
    """Recursively convert non-JSON-serializable values in a result dict."""
    import dataclasses

    cleaned: dict[str, Any] = {}
    for key, value in result.items():
        if key == "errors":
            cleaned[key] = [
                {k: str(v) for k, v in e.items()} if isinstance(e, dict) else str(e)
                for e in (value or [])
            ]
        elif hasattr(value, "model_dump"):
            cleaned[key] = value.model_dump()
        elif dataclasses.is_dataclass(value) and not isinstance(value, type):
            cleaned[key] = dataclasses.asdict(value)
        elif isinstance(value, dict):
            cleaned[key] = _serialize_result(value)
        elif isinstance(value, list):
            cleaned[key] = [
                v.model_dump()
                if hasattr(v, "model_dump")
                else dataclasses.asdict(v)
                if (dataclasses.is_dataclass(v) and not isinstance(v, type))
                else v
                for v in value
            ]
        else:
            cleaned[key] = value
    return cleaned
