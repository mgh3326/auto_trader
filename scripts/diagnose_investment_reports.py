#!/usr/bin/env python
"""ROB-1488 — investment_report_* writer health. Read-only.

The 2026-07-28 report writer stall was found 3 months late, from the inventory
pass. This script makes the same measurement a command:

    ENV_FILE=... uv run python -m scripts.diagnose_investment_reports
    uv run python -m scripts.diagnose_investment_reports --json

It issues SELECTs only -- no INSERT/UPDATE/DELETE, no DDL, no broker call, no
scheduler registration. It never creates a report and never infers one from a
watch: the report and watch surfaces are measured separately, because they are
separate writers (``investment_watch_create``, ROB-768, writes watches without
a report).

Link counts are resolved, not raw. ``review.investment_watch_alerts`` and
``..._events`` may hold legacy UUIDv5 placeholder links that the ROB-1103
migration deliberately preserved (NOT VALID foreign keys); a non-null
``source_report_uuid`` is therefore not proof of a report. Only a value that
matches ``review.investment_reports.report_uuid`` is counted as a link, and
everything else non-null is reported as ``dangling``.

Exit code is 0 when the report writer is within ``STALL_THRESHOLD_DAYS``,
1 when it is stale or has never written, so a check can gate on it.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import case, exists, func, select, text

from app.core.db import AsyncSessionLocal
from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)

STALL_THRESHOLD_DAYS = 14
WATCH_LOOKBACK_DAYS = 30
LINK_CLASSES = ("null", "resolved", "dangling")


# ---------------------------------------------------------------------------
# Pure classification (no DB) — the tested part
# ---------------------------------------------------------------------------
def writer_health(
    last_created: datetime | None,
    *,
    now: datetime,
    threshold_days: int = STALL_THRESHOLD_DAYS,
) -> dict[str, Any]:
    """Classify the writer from its last write.

    ``never`` = no row at all; ``stale`` = the last write is older than the
    threshold; ``ok`` otherwise. The threshold is a visibility device, not a
    declared business cadence — no report cadence is declared in the repo.
    """
    if last_created is None:
        return {"status": "never", "stale_days": None}
    age = now - last_created
    stale_days = max(0, int(age.total_seconds() // 86400))
    return {
        "status": "stale" if age > timedelta(days=threshold_days) else "ok",
        "stale_days": stale_days,
    }


def watch_linkage_verdict(
    *,
    lookback_null: int,
    lookback_resolved: int,
    lookback_dangling: int,
    active_dangling: int,
) -> dict[str, Any]:
    """Separate "watches are in use" from "reports are being written".

    A delivered watch event only carries a report link when the alert it fired
    from was activated from a report item; ROB-768 direct watches carry none by
    design. The diagnostic reports only the links observed in its lookback window;
    it does not establish that no other report dependency exists.
    """
    if lookback_resolved > 0:
        verdict = "report_linked_events"
    elif lookback_null > 0 or lookback_dangling > 0:
        verdict = "no_report_linked_events"
    else:
        verdict = "no_recent_events"
    return {
        "verdict": verdict,
        "active_dangling_alerts": active_dangling,
        "dangling_links_are_legacy_placeholders": True,
        "recent_report_linked_events_observed": lookback_resolved > 0,
    }


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------
def _link_class_case(model: Any) -> Any:
    resolved = exists(
        select(InvestmentReport.id).where(
            InvestmentReport.report_uuid == model.source_report_uuid
        )
    )
    return case(
        (model.source_report_uuid.is_(None), "null"),
        (resolved, "resolved"),
        else_="dangling",
    )


def _empty_link_map() -> dict[str, dict[str, Any]]:
    return {name: {"rows": 0, "last_created": None} for name in LINK_CLASSES}


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


async def _table_health(db: Any, model: Any, now: datetime) -> dict[str, Any]:
    window_start = now - timedelta(days=30)
    kst_day = func.date(func.timezone("Asia/Seoul", model.created_at))
    row = (
        await db.execute(
            select(
                func.count().label("rows"),
                func.min(model.created_at).label("first_created"),
                func.max(model.created_at).label("last_created"),
                func.count().filter(model.created_at >= window_start).label("rows_30d"),
                func.count(func.distinct(kst_day))
                .filter(model.created_at >= window_start)
                .label("days_30d"),
            )
        )
    ).one()
    return {
        "rows": row.rows,
        "first_created": _iso(row.first_created),
        "last_created": _iso(row.last_created),
        "rows_30d": row.rows_30d,
        "days_30d": row.days_30d,
        **writer_health(row.last_created, now=now),
    }


async def _report_status(db: Any) -> dict[str, int]:
    rows = (
        await db.execute(
            select(InvestmentReport.status, func.count().label("rows")).group_by(
                InvestmentReport.status
            )
        )
    ).all()
    return {r.status: r.rows for r in rows}


async def _report_profiles(db: Any) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(
                InvestmentReport.created_by_profile,
                func.count().label("rows"),
                func.min(InvestmentReport.created_at).label("first_created"),
                func.max(InvestmentReport.created_at).label("last_created"),
            )
            .group_by(InvestmentReport.created_by_profile)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "created_by_profile": r.created_by_profile,
            "rows": r.rows,
            "first_created": _iso(r.first_created),
            "last_created": _iso(r.last_created),
        }
        for r in rows
    ]


async def _links_by_class(
    db: Any, model: Any, *, where: Any | None = None
) -> dict[str, dict[str, Any]]:
    link_class = _link_class_case(model)
    stmt = select(
        link_class.label("link_class"),
        func.count().label("rows"),
        func.max(model.created_at).label("last_created"),
    ).group_by(link_class)
    if where is not None:
        stmt = stmt.where(where)
    result = _empty_link_map()
    for row in (await db.execute(stmt)).all():
        result[row.link_class] = {
            "rows": row.rows,
            "last_created": _iso(row.last_created),
        }
    return result


async def _events_last_delivered(db: Any) -> datetime | None:
    return (
        await db.execute(select(func.max(InvestmentWatchEvent.delivered_at)))
    ).scalar_one()


async def collect(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    window_start = now - timedelta(days=WATCH_LOOKBACK_DAYS)
    async with AsyncSessionLocal() as db:
        # SELECT-only enforcement: the transaction itself refuses any write.
        await db.execute(text("SET TRANSACTION READ ONLY"))
        await db.execute(text("SET LOCAL statement_timeout = '8000'"))
        reports = await _table_health(db, InvestmentReport, now)
        items = await _table_health(db, InvestmentReportItem, now)
        decisions = await _table_health(db, InvestmentReportItemDecision, now)
        alerts = await _table_health(db, InvestmentWatchAlert, now)
        events = await _table_health(db, InvestmentWatchEvent, now)
        active_window = InvestmentWatchAlert.status == "active"
        alerts_by_link = await _links_by_class(db, InvestmentWatchAlert)
        active_alerts_by_link = await _links_by_class(
            db, InvestmentWatchAlert, where=active_window
        )
        events_lookback_by_link = await _links_by_class(
            db,
            InvestmentWatchEvent,
            where=InvestmentWatchEvent.created_at >= window_start,
        )
        events_by_link = await _links_by_class(db, InvestmentWatchEvent)
        last_delivered = await _events_last_delivered(db)
        status_counts = await _report_status(db)
        profiles = await _report_profiles(db)

    linkage = watch_linkage_verdict(
        lookback_null=events_lookback_by_link["null"]["rows"],
        lookback_resolved=events_lookback_by_link["resolved"]["rows"],
        lookback_dangling=events_lookback_by_link["dangling"]["rows"],
        active_dangling=active_alerts_by_link["dangling"]["rows"],
    )
    return {
        "as_of": now.isoformat(),
        "stall_threshold_days": STALL_THRESHOLD_DAYS,
        "watch_lookback_days": WATCH_LOOKBACK_DAYS,
        "report_writer": reports,
        "report_items": items,
        "report_item_decisions": decisions,
        "report_status_counts": status_counts,
        "report_profiles": profiles,
        "watch_alerts": {
            **alerts,
            "active": active_alerts_by_link,
            "by_link": alerts_by_link,
        },
        "watch_events": {
            **events,
            "last_delivered": _iso(last_delivered),
            "by_link": events_by_link,
            "lookback_by_link": events_lookback_by_link,
        },
        "watch_linkage": linkage,
        "writer_ok": reports["status"] == "ok",
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _fmt(value: Any) -> str:
    return "-" if value is None else str(value)


def render_text(payload: dict[str, Any]) -> str:
    reports = payload["report_writer"]
    lines = [
        f"investment_report_* writer health — as of {payload['as_of']}",
        (
            f"stall_threshold={payload['stall_threshold_days']}d  "
            f"watch_lookback={payload['watch_lookback_days']}d"
        ),
        "",
        (
            f"report writer        status={reports['status']:<6} "
            f"stale_days={_fmt(reports['stale_days']):<5} "
            f"rows={reports['rows']:<5} last={_fmt(reports['last_created'])} "
            f"last30d_rows={reports['rows_30d']}"
        ),
        (
            f"report items         rows={payload['report_items']['rows']:<5} "
            f"last={_fmt(payload['report_items']['last_created'])}"
        ),
        (
            f"item decisions       rows={payload['report_item_decisions']['rows']:<5} "
            f"last={_fmt(payload['report_item_decisions']['last_created'])}"
        ),
        (
            "report status        "
            + " ".join(
                f"{k}={v}" for k, v in sorted(payload["report_status_counts"].items())
            )
        ),
    ]
    alerts = payload["watch_alerts"]
    events = payload["watch_events"]
    lines += [
        "",
        (
            f"watch alerts         rows={alerts['rows']:<5} "
            f"last={_fmt(alerts['last_created'])}"
        ),
        "  active by link     "
        + " ".join(f"{k}={alerts['active'][k]['rows']}" for k in LINK_CLASSES),
        (
            f"watch events         rows={events['rows']:<5} "
            f"last={_fmt(events['last_created'])} "
            f"last_delivered={_fmt(events['last_delivered'])}"
        ),
        "  lookback by link   "
        + " ".join(
            f"{k}={events['lookback_by_link'][k]['rows']}" for k in LINK_CLASSES
        ),
        "",
        (
            f"watch linkage        {payload['watch_linkage']['verdict']} "
            f"(active dangling alerts="
            f"{payload['watch_linkage']['active_dangling_alerts']})"
        ),
    ]
    if payload["report_profiles"]:
        lines.append("")
        lines.append("report profiles")
        for row in payload["report_profiles"]:
            lines.append(
                f"  {row['created_by_profile']:<24} n={row['rows']:<4} "
                f"first={_fmt(row['first_created'])} last={_fmt(row['last_created'])}"
            )
    lines.append("")
    if payload["writer_ok"]:
        lines.append("OK — the report writer is within the stall threshold.")
    else:
        lines.append(
            "STALE — no in-repo scheduled producer is registered for investment_report_*. "
            "External sessions can call the in-repo writer entrypoints "
            "(investment_report_create / the gated snapshot generator / Hermes "
            "ingest) plus the default-disabled manual runner "
            "scripts.invest_reports_us_schedule. See "
            "docs/runbooks/investment-report-writer-health.md."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable payload"
    )
    args = parser.parse_args(argv)
    payload = asyncio.run(collect())
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    else:
        print(render_text(payload))
    return 0 if payload["writer_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
