#!/usr/bin/env python
"""ROB-1283 — reproduce the negative-class recording diagnosis. Read-only.

The 2026-06-15 stall was found once, by hand, 66 days late. This script exists
so the same measurement is a command rather than an archaeology session:

    ENV_FILE=... uv run python -m scripts.diagnose_negative_class_recording

It issues SELECTs only -- no INSERT/UPDATE/DELETE, no DDL, no broker call, no
scheduler registration. It never backfills and never infers: a stretch of time
with no records is printed as a gap with its real endpoints.

Exit code is 0 when the negative class is recording, 1 when it is stalled or
has never recorded, so a future check can gate on it. ``--json`` prints the
machine-readable payload (the same shape ``get_operating_briefing`` embeds
under ``negative_class_recording``).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select

from app.core.db import AsyncSessionLocal
from app.models.investment_reports import InvestmentReport, InvestmentReportItem
from app.models.review import TradeForecast
from app.services.trade_journal.negative_class import (
    NEGATIVE_CLASS_BUCKET,
    STALL_THRESHOLD_DAYS,
    load_negative_class_health,
)

MARKETS = ("kr", "us", "crypto")


async def _surface_timeline(db: Any) -> dict[str, Any]:
    """Per-month counts on both surfaces — the evidence the root cause rests on."""
    # Truncate in UTC explicitly: date_trunc on a timestamptz uses the session
    # TimeZone, which silently shifts a month boundary (a 08-01T00:20Z row
    # reports as 2026-07 under a negative-offset session) — exactly the kind of
    # off-by-one that made this stall hard to date in the first place.
    item_month = func.date_trunc(
        "month", func.timezone("UTC", InvestmentReportItem.created_at)
    )
    items = (
        await db.execute(
            select(
                item_month.label("month"),
                func.count().label("n"),
                func.count(InvestmentReportItem.decision_bucket).label("bucketed"),
            )
            .group_by(item_month)
            .order_by(item_month)
        )
    ).all()

    fc_month = func.date_trunc("month", func.timezone("UTC", TradeForecast.created_at))
    forecasts = (
        await db.execute(
            select(
                fc_month.label("month"),
                func.count().label("n"),
                func.count(TradeForecast.decision_bucket).label("bucketed"),
                func.count(TradeForecast.report_item_uuid).label("linked"),
            )
            .group_by(fc_month)
            .order_by(fc_month)
        )
    ).all()
    return {
        "report_items_by_month": [
            {"month": r.month.strftime("%Y-%m"), "items": r.n, "bucketed": r.bucketed}
            for r in items
        ],
        "forecasts_by_month": [
            {
                "month": r.month.strftime("%Y-%m"),
                "forecasts": r.n,
                "bucketed": r.bucketed,
                "linked_to_report_item": r.linked,
            }
            for r in forecasts
        ],
    }


async def _bucket_spans(db: Any) -> list[dict[str, Any]]:
    """First/last timestamp per decision_bucket on the report-item surface."""
    rows = (
        await db.execute(
            select(
                InvestmentReportItem.decision_bucket,
                func.count().label("n"),
                func.min(InvestmentReportItem.created_at).label("first"),
                func.max(InvestmentReportItem.created_at).label("last"),
            )
            .join(
                InvestmentReport,
                InvestmentReportItem.report_id == InvestmentReport.id,
            )
            .group_by(InvestmentReportItem.decision_bucket)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        {
            "decision_bucket": r.decision_bucket or "unclassified",
            "count": r.n,
            "first": r.first.isoformat() if r.first else None,
            "last": r.last.isoformat() if r.last else None,
        }
        for r in rows
    ]


async def run(as_json: bool) -> int:
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        health = {
            market: (
                await load_negative_class_health(db, market=market, now=now)
            ).to_dict()
            for market in MARKETS
        }
        payload = {
            "as_of": now.isoformat(),
            "negative_class_bucket": NEGATIVE_CLASS_BUCKET,
            "stall_threshold_days": STALL_THRESHOLD_DAYS,
            "health_by_market": health,
            "report_item_bucket_spans": await _bucket_spans(db),
            **await _surface_timeline(db),
        }

    stalled = [m for m, h in health.items() if h["status"] != "ok"]
    payload["stalled_markets"] = stalled

    if as_json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 1 if stalled else 0

    print(f"ROB-1283 negative-class recording — as of {payload['as_of']}")
    print(f"bucket={NEGATIVE_CLASS_BUCKET} stall_threshold={STALL_THRESHOLD_DAYS}d\n")

    print("per-market health")
    for market, h in health.items():
        print(
            f"  {market:<7} status={h['status']:<15} "
            f"last={h['last_recorded_at'] or '-'} "
            f"source={h['last_source'] or '-'} stale_days={h['stale_days']}"
        )
        gap = h.get("gap")
        if gap:
            end = gap["ends_at"] or "OPEN (still no replacement record)"
            print(
                f"          GAP {gap['days']}d  {gap['starts_at']} -> {end}"
                f"  backfilled={gap['backfilled']}"
            )

    print("\nreport-item decision_bucket spans")
    for row in payload["report_item_bucket_spans"]:
        print(
            f"  {row['decision_bucket']:<24} n={row['count']:<5} "
            f"{row['first']} .. {row['last']}"
        )

    print("\nreport items by month (surface A)")
    for row in payload["report_items_by_month"]:
        print(f"  {row['month']}  items={row['items']:<5} bucketed={row['bucketed']}")

    print("\nforecasts by month (surface B)")
    for row in payload["forecasts_by_month"]:
        print(
            f"  {row['month']}  forecasts={row['forecasts']:<5} "
            f"bucketed={row['bucketed']:<5} "
            f"linked_to_report_item={row['linked_to_report_item']}"
        )

    if stalled:
        print(
            "\nSTALLED: " + ", ".join(stalled) + " — see "
            "docs/runbooks/negative-class-recording.md"
        )
    else:
        print("\nOK — every market has a recent negative-class record.")
    return 1 if stalled else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable payload"
    )
    args = parser.parse_args()
    return asyncio.run(run(args.json))


if __name__ == "__main__":
    sys.exit(main())
