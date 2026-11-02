#!/usr/bin/env python
"""ROB-1488 — reproduce the manual approval batch health measurement. Read-only.

The 2026-11-02 inventory reported "approval digest members stopped 2026-09-08"
by reading ``processed_at`` (the callback result recorder) instead of
``added_at`` (the membership writer). This script makes the correct
measurement a command:

    ENV_FILE=... uv run python -m scripts.diagnose_approval_batch_health

It issues SELECTs only -- no INSERT/UPDATE/DELETE, no DDL, no broker or
Telegram call, no scheduler registration. It never backfills and never infers:
a quiet surface is printed as quiet.

Exit code is 0 when the hard invariants hold, 1 when a batch has no member or
the member writer stalled while manual approval cards were still published.
``--json`` prints the machine-readable payload.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime

from app.core.db import AsyncSessionLocal
from app.services.order_proposals.batch_health import (
    DEFAULT_RECENT_DAYS,
    DEFAULT_WINDOW_DAYS,
    BatchHealthReport,
    load_batch_health,
)


def _print_window(report: BatchHealthReport) -> None:
    for label, window in (("recent", report.recent), ("window", report.window)):
        print(
            f"{label} ({window.days}d): manual_cards={window.manual_cards} "
            f"member_added={window.member_added} "
            f"member_processed={window.member_processed} "
            f"batches={window.batches} summaries_sent={window.summaries_sent} "
            f"frozen_unsent={window.frozen_unsent} "
            f"published_cards_without_member={window.published_cards_without_member}"
        )


def _print_report(report: BatchHealthReport) -> None:
    print(f"ROB-1488 approval batch health — as of {report.as_of.isoformat()}")
    print(
        f"members_total={report.members_total} "
        f"zero_member_batches={report.zero_member_batches}"
    )
    _print_window(report)
    print(
        "\nmember_writer_column=added_at "
        "(processed_at is the callback result recorder, not the writer)"
    )
    print(
        "ROB-1052 auto-approve digest writes no batch/member rows; a single-member "
        "idle batch is expected, not a lost card."
    )
    if report.anomalies:
        print("\nGAP: " + ", ".join(report.anomalies))
    else:
        print("\nOK — every batch has a member and the writer follows published cards.")


async def run(
    *,
    as_json: bool = False,
    recent_days: int = DEFAULT_RECENT_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
) -> int:
    async with AsyncSessionLocal() as db:
        report = await load_batch_health(
            db,
            now=now or datetime.now(UTC),
            recent_days=recent_days,
            window_days=window_days,
        )
    if as_json:
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        _print_report(report)
    return 1 if report.anomalies else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json", action="store_true", help="emit the machine-readable payload"
    )
    parser.add_argument(
        "--recent-days",
        type=int,
        default=DEFAULT_RECENT_DAYS,
        help=f"stall lookback in days (default {DEFAULT_RECENT_DAYS})",
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=DEFAULT_WINDOW_DAYS,
        help=f"context lookback in days (default {DEFAULT_WINDOW_DAYS})",
    )
    args = parser.parse_args()
    return asyncio.run(
        run(
            as_json=args.json,
            recent_days=args.recent_days,
            window_days=args.window_days,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
