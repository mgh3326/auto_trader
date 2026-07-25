#!/usr/bin/env python3
"""ROB-1050 — Read-only CLI for live order ledger periodic reconcile backfill report.

Queries all unreconciled live order ledger rows across US, Crypto, and KR markets,
classifying them by broker lookback window (KIS 90d, Upbit UUID lookup).

STRICT READ-ONLY: Never mutates DB or triggers reconcile execution.

Usage:
    uv run python scripts/live_reconcile_backfill_report.py
    uv run python scripts/live_reconcile_backfill_report.py --market us
    uv run python scripts/live_reconcile_backfill_report.py --market crypto
    uv run python scripts/live_reconcile_backfill_report.py --market kr
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select

from app.mcp_server.tooling.kis_live_ledger import _order_session_factory
from app.models.review import KISLiveOrderLedger, LiveOrderLedger
from app.services.live_reconcile_metrics import OPEN_STATUSES, _row_timestamp


def _classify_lookback_window(market: str, broker: str, age_days: float) -> str:
    if broker == "upbit" or market == "crypto":
        return "within_window (UUID lookup)"

    # KIS broker (US or KR): 90-day lookback limit
    if age_days <= 90.0:
        return "within_window (<=90d)"
    return "outside_window (>90d)"


async def generate_backfill_report(market_filter: str = "all") -> dict[str, Any]:
    now_ref = datetime.now(UTC)
    items: list[dict[str, Any]] = []

    async with _order_session_factory()() as db:
        if market_filter in ("all", "us", "crypto"):
            stmt = select(LiveOrderLedger).where(
                LiveOrderLedger.reconciled_at.is_(None),
                LiveOrderLedger.status.in_(OPEN_STATUSES),
            )
            if market_filter != "all":
                stmt = stmt.where(LiveOrderLedger.market == market_filter)

            live_rows = list((await db.execute(stmt)).scalars().all())
            for r in live_rows:
                ts = _row_timestamp(r)
                age_seconds = (now_ref - ts).total_seconds() if ts else 0.0
                age_days = age_seconds / 86400.0
                mkt = str(r.market).lower() if r.market else "unknown"
                brk = str(r.broker).lower() if r.broker else "unknown"
                window_status = _classify_lookback_window(mkt, brk, age_days)

                items.append(
                    {
                        "table": "review.live_order_ledger",
                        "id": r.id,
                        "market": mkt,
                        "broker": brk,
                        "symbol": r.symbol,
                        "side": r.side,
                        "order_no": r.order_no,
                        "status": r.status,
                        "created_at": ts.isoformat() if ts else "N/A",
                        "age_days": round(age_days, 1),
                        "window_status": window_status,
                    }
                )

        if market_filter in ("all", "kr"):
            stmt = select(KISLiveOrderLedger).where(
                KISLiveOrderLedger.reconciled_at.is_(None),
                KISLiveOrderLedger.status.in_(OPEN_STATUSES),
            )
            kis_rows = list((await db.execute(stmt)).scalars().all())
            for r in kis_rows:
                ts = _row_timestamp(r)
                age_seconds = (now_ref - ts).total_seconds() if ts else 0.0
                age_days = age_seconds / 86400.0
                mkt = "kr"
                brk = str(r.broker).lower() if r.broker else "kis"
                window_status = _classify_lookback_window(mkt, brk, age_days)

                items.append(
                    {
                        "table": "review.kis_live_order_ledger",
                        "id": r.id,
                        "market": mkt,
                        "broker": brk,
                        "symbol": r.symbol,
                        "side": r.side,
                        "order_no": r.order_no,
                        "status": r.status,
                        "created_at": ts.isoformat() if ts else "N/A",
                        "age_days": round(age_days, 1),
                        "window_status": window_status,
                    }
                )

    # Sort items by age_days descending (oldest first)
    items.sort(key=lambda x: x["age_days"], reverse=True)

    summary_by_market: dict[str, dict[str, int]] = {}
    summary_by_window: dict[str, int] = {}

    for item in items:
        m = item["market"]
        w = item["window_status"]
        if m not in summary_by_market:
            summary_by_market[m] = {"total": 0, "within_window": 0, "outside_window": 0}
        summary_by_market[m]["total"] += 1
        if "within_window" in w:
            summary_by_market[m]["within_window"] += 1
        else:
            summary_by_market[m]["outside_window"] += 1

        summary_by_window[w] = summary_by_window.get(w, 0) + 1

    return {
        "as_of": now_ref.isoformat(),
        "market_filter": market_filter,
        "total_backlog": len(items),
        "summary_by_market": summary_by_market,
        "summary_by_window": summary_by_window,
        "items": items,
    }


def print_report(data: dict[str, Any]) -> None:
    print("=" * 85)
    print(f"LIVE ORDER LEDGER RECONCILE BACKFILL REPORT (As of: {data['as_of']})")
    print(
        f"Market Filter: {data['market_filter']} | Total Unreconciled Backlog: {data['total_backlog']}"
    )
    print("=" * 85)

    print("\n--- Summary by Market ---")
    for mkt, counts in data["summary_by_market"].items():
        print(
            f"  {mkt.upper():<8}: Total={counts['total']:<4} | "
            f"Within Broker Window={counts['within_window']:<4} | "
            f"Outside Window (>90d)={counts['outside_window']:<4}"
        )

    print("\n--- Summary by Window Status ---")
    for w_status, count in data["summary_by_window"].items():
        print(f"  {w_status:<30}: {count} order(s)")

    print("\n--- Detailed Backlog Items ---")
    header = (
        f"{'ID':<6} {'Mkt':<6} {'Broker':<6} {'Symbol':<10} {'Side':<5} "
        f"{'Age(d)':<8} {'Window Status':<25} {'Order No':<20}"
    )
    print(header)
    print("-" * 85)
    for item in data["items"]:
        print(
            f"{item['id']:<6} {item['market']:<6} {item['broker']:<6} {item['symbol']:<10} "
            f"{item['side']:<5} {item['age_days']:<8.1f} {item['window_status']:<25} "
            f"{str(item['order_no'])[:20]:<20}"
        )
    print("=" * 85)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ROB-1050 live order ledger backfill report CLI"
    )
    parser.add_argument(
        "--market",
        choices=["all", "us", "crypto", "kr"],
        default="all",
        help="Filter by market (default: all)",
    )
    args = parser.parse_args()

    report = asyncio.run(generate_backfill_report(args.market))
    print_report(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
