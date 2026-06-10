"""Dry-run-first CLI for ROB-211 execution ledger reconciliation."""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime, time

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.reconciler import ExecutionLedgerReconciler
from app.services.execution_ledger.repository import ExecutionLedgerRepository


def _parse_cli_date(value: str, *, end_of_day: bool) -> datetime:
    raw = value.strip()
    fmt = "%Y%m%d" if len(raw) == 8 and raw.isdigit() else "%Y-%m-%d"
    day = datetime.strptime(raw, fmt).date()
    boundary = time.max if end_of_day else time.min
    return datetime.combine(day, boundary, tzinfo=UTC)


def resolve_window_args(
    args: argparse.Namespace,
) -> tuple[datetime | None, datetime | None]:
    if args.start_date is None and args.end_date is None:
        return None, None
    if args.start_date is None or args.end_date is None:
        raise ValueError("--start-date and --end-date must be provided together")
    start_at = _parse_cli_date(args.start_date, end_of_day=False)
    end_at = _parse_cli_date(args.end_date, end_of_day=True)
    if start_at >= end_at:
        raise ValueError("--start-date must be before --end-date")
    return start_at, end_at


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reconcile broker filled orders into the execution ledger."
    )
    parser.add_argument("--broker", choices=["kis", "upbit"], required=True)
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--start-date", help="UTC date YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--end-date", help="UTC date YYYY-MM-DD or YYYYMMDD")
    parser.add_argument("--max-pages", type=int, default=100)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--commit", action="store_true")
    return parser.parse_args()


async def _main() -> int:
    args = parse_args()
    dry_run = not bool(args.commit)
    start_at, end_at = resolve_window_args(args)
    async with AsyncSessionLocal() as db:
        reconciler = ExecutionLedgerReconciler(ExecutionLedgerRepository(db))
        try:
            diff = await reconciler.run(
                args.broker,
                window_hours=args.window_hours,
                start_at=start_at,
                end_at=end_at,
                max_pages=args.max_pages,
                dry_run=dry_run,
            )
        except Exception:
            if dry_run:
                # Dry-run skips ledger upserts; commit only preserves the run audit row.
                await db.commit()
            else:
                await db.rollback()
            raise
        # Dry-run skips ledger upserts; commit only preserves the run audit row.
        await db.commit()
    print(json.dumps(diff.model_dump(mode="json"), ensure_ascii=False, sort_keys=True))
    return 0


def main() -> int:
    return asyncio.run(_main())


if __name__ == "__main__":
    raise SystemExit(main())
