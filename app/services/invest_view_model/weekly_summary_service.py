"""ROB-144 — weekly summary composer from existing market_reports."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.invest_calendar import WeeklySection, WeeklySummaryResponse
from app.services.market_report_service import get_market_reports

REPORT_TYPES = ("daily_brief", "kr_morning", "crypto_scan")


async def build_weekly_summary(
    *,
    db: AsyncSession,  # noqa: ARG001  — kept for API consistency; service uses own session
    week_start: date,
) -> WeeklySummaryResponse:
    week_end = week_start + timedelta(days=6)
    sections: list[WeeklySection] = []
    seen_dates: set[date] = set()

    for rt in REPORT_TYPES:
        # get_market_reports manages its own DB session (no db param), returns list[dict]
        reports = await get_market_reports(report_type=rt, days=14)
        for r in reports:
            # report_date is stored as isoformat string in the dict
            r_date_raw = r.get("report_date")
            if not r_date_raw:
                continue
            if isinstance(r_date_raw, date):
                r_date = r_date_raw
            else:
                try:
                    r_date = date.fromisoformat(str(r_date_raw))
                except ValueError:
                    continue
            if not (week_start <= r_date <= week_end):
                continue
            seen_dates.add(r_date)
            # MarketReport dict has 'summary' (text), not 'body_md' or 'body'
            body = str(r.get("summary") or "")
            sections.append(
                WeeklySection(
                    date=r_date,
                    reportType=rt,
                    market=r.get("market"),
                    title=str(r.get("title") or f"{rt} {r_date.isoformat()}"),
                    body=body,
                )
            )

    sections.sort(key=lambda s: (s.date, s.reportType))
    all_dates = {week_start + timedelta(days=i) for i in range(7)}
    missing = sorted(all_dates - seen_dates)
    return WeeklySummaryResponse(
        weekStart=week_start,
        asOf=datetime.now(UTC),
        sections=sections,
        partial=bool(missing),
        missingDates=missing,
    )
